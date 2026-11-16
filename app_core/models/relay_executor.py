"""Relay execution across a model strategy chain.

The executor is transport-agnostic: ``call_batch`` is supplied by the caller
(API adapter, queue adapter, web bridge, ...).  It implements the V2.3 relay
contract: queue timeout/rate-limit hand-off, partial success, cross-capacity
re-packing, and whole-chain cooldown/retry.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable, Sequence

from models.endpoint_pool import EndpointNode, EndpointPool, ModelStrategy
from parser.batch_packer import DistilledItem, DynamicBatch, DynamicBatchPacker


class RelayError(RuntimeError):
    pass


class QueueTimeout(RelayError):
    pass


class NodeBusy(RelayError):
    pass


class NodeFailed(RelayError):
    pass


@dataclass
class BatchOutcome:
    """Result of one node call.

    ``failed_items`` is intentionally explicit: a 20-item request may return
    15 valid items and 5 malformed ones, and the 5 must return to the queue.
    """

    success_items: list[DistilledItem] = field(default_factory=list)
    failed_items: list[DistilledItem] = field(default_factory=list)
    payloads: list[Any] = field(default_factory=list)


@dataclass
class RelayResult:
    completed: list[Any] = field(default_factory=list)
    failed_items: list[DistilledItem] = field(default_factory=list)
    attempts: list[dict[str, Any]] = field(default_factory=list)
    cancelled: bool = False
    exhausted: bool = False


class QueuePoller:
    """Poll a queue-style endpoint every N seconds.

    ``get_status`` returns a mapping with at least ``status``.  ``queued`` /
    ``pending`` count against the queue deadline; once ``processing`` or
    ``running`` is observed the queue deadline is removed and polling continues
    until completion.
    """

    QUEUED = {"queued", "pending", "waiting", "submitted"}
    RUNNING = {"processing", "running", "in_progress", "started"}
    DONE = {"done", "completed", "succeeded", "success", "finished"}
    FAILED = {"failed", "error", "cancelled", "canceled", "429", "503"}

    def __init__(
        self,
        *,
        sleep: Callable[[float], None] | None = None,
        clock: Callable[[], float] | None = None,
        emit: Callable[[str], None] | None = None,
    ):
        import time

        self.sleep = sleep or time.sleep
        self.clock = clock or time.monotonic
        self.emit = emit

    def wait(
        self,
        node: EndpointNode,
        get_status: Callable[[], dict[str, Any]],
        *,
        should_stop: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        started = self.clock()
        queued_since = started
        processing = False
        while True:
            if should_stop is not None and should_stop():
                raise NodeFailed("用户已取消")
            status = dict(get_status() or {})
            state = str(status.get("status") or "").lower().strip()
            elapsed = self.clock() - started
            if state in self.DONE:
                return status
            if state in self.FAILED:
                raise NodeFailed(
                    f"节点 {node.name} 返回 {state}: "
                    f"{status.get('message') or status.get('error') or ''}"
                )
            if state in self.RUNNING:
                if not processing:
                    processing = True
                    if self.emit:
                        self.emit(
                            f"⚙️ 节点 [{node.name}] 已进入处理中，持续等待结果…"
                        )
            elif state in self.QUEUED or not state:
                if processing:
                    # A transient status regression should not re-enable the
                    # queue timeout after processing has begun.
                    pass
                else:
                    waited = self.clock() - queued_since
                    if self.emit:
                        self.emit(
                            f"⏳ 节点 [{node.name}] 正在排队中 "
                            f"(已等待 {int(waited)}s / 阈值 {node.max_queue_wait_sec}s)…"
                        )
                    if waited >= node.max_queue_wait_sec:
                        raise QueueTimeout(
                            f"节点 {node.name} 排队超过 {node.max_queue_wait_sec}s"
                        )
            else:
                # Unknown states are treated as queued until the deadline.
                waited = self.clock() - queued_since
                if not processing and waited >= node.max_queue_wait_sec:
                    raise QueueTimeout(f"节点 {node.name} 状态 {state} 超时")
            remaining = node.poll_interval_sec
            if not processing:
                remaining = min(
                    remaining,
                    max(0.0, node.max_queue_wait_sec - (self.clock() - queued_since)),
                )
            self.sleep(max(0.0, remaining))


class RelayExecutor:
    """Run a pending item queue through a strategy's active node chain."""

    def __init__(
        self,
        pool: EndpointPool,
        packer: DynamicBatchPacker | None = None,
        *,
        sleep: Callable[[float], None] | None = None,
        now_provider: Callable[[], datetime] | None = None,
        emit: Callable[[str], None] | None = None,
    ):
        import time

        self.pool = pool
        self.packer = packer or DynamicBatchPacker()
        self.sleep = sleep or time.sleep
        self.now_provider = now_provider or datetime.now
        self.emit = emit

    def run(
        self,
        items: Iterable[DistilledItem],
        role: str,
        call_batch: Callable[[EndpointNode, DynamicBatch], BatchOutcome],
        *,
        now: datetime | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> RelayResult:
        queue: deque[DistilledItem] = deque(items)
        result = RelayResult(failed_items=[])
        if not queue:
            return result
        current = now or self.now_provider()
        strategy = self.pool.strategy_for_role(role, current.time())
        if strategy is None:
            result.failed_items = list(queue)
            result.exhausted = True
            return result
        max_rounds = (strategy.max_loops + 1) if strategy.enable_loop else 1
        for round_index in range(max_rounds):
            if should_stop is not None and should_stop():
                result.cancelled = True
                break
            nodes = self.pool.chain_for_role(role, current.time())
            if not nodes:
                if self.emit:
                    self.emit("⚠️ 当前策略没有在可用时段内的适配节点")
            chain_had_failure = False
            for node in nodes:
                if not queue:
                    break
                if should_stop is not None and should_stop():
                    result.cancelled = True
                    break
                if self.emit:
                    self.emit(
                        f"🔀 尝试节点 [{node.name}] · {node.capacity_label()}"
                    )
                batches = self.packer.checkout_batches(queue, node)
                node_failed = False
                for batch_index, batch in enumerate(batches):
                    if should_stop is not None and should_stop():
                        self._requeue_batches(
                            queue, [batch, *batches[batch_index + 1:]]
                        )
                        result.cancelled = True
                        node_failed = True
                        break
                    try:
                        outcome = call_batch(node, batch)
                    except (QueueTimeout, NodeBusy, NodeFailed, Exception) as exc:
                        self._requeue_batches(
                            queue, [batch, *batches[batch_index + 1:]]
                        )
                        status = (
                            "queue_timeout"
                            if isinstance(exc, QueueTimeout)
                            else "failed"
                            if isinstance(exc, (NodeBusy, NodeFailed))
                            else "error"
                        )
                        result.attempts.append(
                            {"node": node.node_id, "status": status, "error": str(exc)}
                        )
                        node_failed = True
                        chain_had_failure = True
                        break
                    success = list(getattr(outcome, "success_items", []) or [])
                    failed = list(getattr(outcome, "failed_items", []) or [])
                    success_ids = {item.item_id for item in success}
                    # Anything not explicitly successful stays pending.
                    for item in batch.items:
                        if item.item_id not in success_ids and item not in failed:
                            failed.append(item)
                    if failed:
                        self._requeue_batches(
                            queue,
                            [DynamicBatch(items=failed), *batches[batch_index + 1:]],
                        )
                    result.completed.extend(list(getattr(outcome, "payloads", []) or []))
                    result.attempts.append(
                        {
                            "node": node.node_id,
                            "status": "partial" if failed else "ok",
                            "batch_items": batch.item_count,
                            "success": len(success),
                            "failed": len(failed),
                        }
                    )
                    if failed:
                        # The failed remainder plus not-yet-sent batches are
                        # already back at the queue head; let the next node try.
                        node_failed = True
                        chain_had_failure = True
                        break
                if node_failed:
                    continue
            if not queue:
                break
            if should_stop is not None and should_stop():
                result.cancelled = True
                break
            # Only loop when the whole chain was tried and something remains.
            if not strategy.enable_loop or round_index + 1 >= max_rounds:
                break
            if self.emit:
                self.emit(
                    f"🔁 全链未完成，冷却 {strategy.loop_cooldown_sec}s 后从节点 1 重试 "
                    f"({round_index + 1}/{max_rounds - 1})"
                )
            self.sleep(max(0, strategy.loop_cooldown_sec))
            current = self.now_provider() if now is None else now
        result.failed_items = list(queue)
        result.exhausted = bool(queue) and not result.cancelled
        return result

    def _requeue_batches(
        self,
        queue: deque[DistilledItem],
        batches: Sequence[DynamicBatch],
    ) -> None:
        # Requeue in reverse so the original batch/order ends up at the head.
        for batch in reversed(batches):
            self.packer.requeue(queue, batch)


__all__ = [
    "QueueTimeout",
    "NodeBusy",
    "NodeFailed",
    "BatchOutcome",
    "RelayResult",
    "QueuePoller",
    "RelayExecutor",
]
