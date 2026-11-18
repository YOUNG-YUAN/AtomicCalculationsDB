"""Pure-algorithm dynamic batch packing for heterogeneous model capacities.

No tokenizer, neural network or network call is used here.  The packer is meant
to run immediately before a request so a 5k-Token Web-AI node and a 100k-Token
API node can share the same pending queue and the same relay strategy.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping


_LATIN_RE = re.compile(r"[A-Za-z0-9_]+")
_CJK_RE = re.compile(r"[㐀-鿿]")
_CODE_SYMBOL_RE = re.compile(r"[\\{}$^_\[\]()<>|&+*/=~`#]+")


def estimate_tokens_fast(text: Any) -> int:
    """Fast mixed Chinese/English/LaTeX token estimate (pure Python)."""
    body = str(text or "")
    if not body:
        return 0
    latin = len(_LATIN_RE.findall(body))
    cjk = len(_CJK_RE.findall(body))
    symbols = len(_CODE_SYMBOL_RE.findall(body))
    return max(1, int(latin * 1.3 + cjk * 0.7 + symbols * 1.4))


@dataclass
class DistilledItem:
    """One evidence/paragraph slice waiting for an LLM."""

    item_id: str
    text: str
    est_output_tokens: int = 550
    shared_ref_keys: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    est_input_tokens: int = 0

    def __post_init__(self) -> None:
        self.item_id = str(self.item_id or "item")
        self.text = str(self.text or "")
        self.shared_ref_keys = [
            str(key) for key in (self.shared_ref_keys or []) if str(key).strip()
        ]
        if self.est_input_tokens <= 0:
            self.est_input_tokens = estimate_tokens_fast(self.text)
        self.est_output_tokens = max(0, int(self.est_output_tokens or 0))

    def with_text(self, text: str, suffix: str) -> "DistilledItem":
        return DistilledItem(
            item_id=f"{self.item_id}{suffix}",
            text=text,
            est_output_tokens=self.est_output_tokens,
            shared_ref_keys=list(self.shared_ref_keys),
            metadata=dict(self.metadata),
            est_input_tokens=estimate_tokens_fast(text),
        )


@dataclass
class DynamicBatch:
    items: list[DistilledItem] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    shared_ref_keys: list[str] = field(default_factory=list)

    @property
    def item_count(self) -> int:
        return len(self.items)

    def __bool__(self) -> bool:
        return bool(self.items)


class DynamicBatchPacker:
    """Greedy, capacity-aware deque packer with automatic re-slicing."""

    def __init__(
        self,
        *,
        system_prompt: str = "",
        global_premise: str = "",
        system_tokens: int | None = None,
        global_premise_tokens: int | None = None,
        output_tokens_per_item: int = 550,
        max_split_depth: int = 12,
    ):
        self.system_prompt = str(system_prompt or "")
        self.global_premise = str(global_premise or "")
        self.system_tokens = (
            int(system_tokens)
            if system_tokens is not None
            else estimate_tokens_fast(self.system_prompt)
        )
        self.global_premise_tokens = (
            int(global_premise_tokens)
            if global_premise_tokens is not None
            else estimate_tokens_fast(self.global_premise)
        )
        self.output_tokens_per_item = max(1, int(output_tokens_per_item))
        self.max_split_depth = max(1, int(max_split_depth))

    # ------------------------------------------------------------- public API
    def checkout_batches(
        self,
        pending_queue: deque[DistilledItem] | list[DistilledItem],
        node: Any,
        shared_refs_map: Mapping[str, str] | None = None,
        *,
        system_tokens: int | None = None,
        global_premise_tokens: int | None = None,
    ) -> list[DynamicBatch]:
        """Remove as many batches as fit from the queue for ``node``."""
        queue = pending_queue if isinstance(pending_queue, deque) else deque(pending_queue)
        max_input = int(getattr(node, "max_input_tokens", 32_000) or 32_000)
        max_output = int(getattr(node, "max_output_tokens", 8_000) or 8_000)
        max_items = max(1, int(getattr(node, "max_items_per_batch", 20) or 20))
        base_in = (
            self.system_tokens if system_tokens is None else int(system_tokens)
        ) + (
            self.global_premise_tokens
            if global_premise_tokens is None
            else int(global_premise_tokens)
        )
        if max_input <= base_in:
            raise ValueError(
                f"节点输入上限 {max_input} 小于固定系统/全局提示 {base_in}"
            )
        refs = {str(key): str(value) for key, value in (shared_refs_map or {}).items()}
        batches: list[DynamicBatch] = []
        while queue:
            first = queue[0]
            refs_tokens = self._refs_tokens(first, refs)
            if base_in + refs_tokens >= max_input:
                # Shared references alone are wider than this node.  Emit the
                # item as a singleton so the relay can move it to a larger node
                # instead of looping forever on an impossible split.
                queue.popleft()
                batches.append(
                    DynamicBatch(
                        items=[first],
                        input_tokens=base_in + refs_tokens + first.est_input_tokens,
                        output_tokens=first.est_output_tokens,
                        shared_ref_keys=list(first.shared_ref_keys),
                    )
                )
                continue
            if base_in + refs_tokens + first.est_input_tokens > max_input:
                queue.popleft()
                chunks = self._split_item(
                    first,
                    max(1, max_input - base_in - refs_tokens),
                    depth=0,
                )
                queue.extendleft(reversed(chunks))
                continue
            batch = DynamicBatch()
            batch_keys: set[str] = set()
            batch_refs_tokens = 0
            while queue:
                item = queue[0]
                additional_keys = set(item.shared_ref_keys) - batch_keys
                additional_ref_tokens = sum(
                    estimate_tokens_fast(refs.get(key, "")) for key in additional_keys
                )
                next_input = (
                    base_in
                    + batch_refs_tokens
                    + additional_ref_tokens
                    + sum(entry.est_input_tokens for entry in batch.items)
                    + item.est_input_tokens
                )
                next_output = (
                    sum(entry.est_output_tokens for entry in batch.items)
                    + item.est_output_tokens
                )
                if batch.items and (
                    len(batch.items) >= max_items
                    or next_input > max_input
                    or next_output > max_output
                ):
                    break
                if not batch.items and next_output > max_output:
                    # A single item's reserved output exceeds this node: still
                    # send it alone, but split if its input is the blocker.
                    if item.est_input_tokens + base_in + batch_refs_tokens > max_input:
                        break
                queue.popleft()
                batch.items.append(item)
                batch_keys.update(item.shared_ref_keys)
                batch_refs_tokens = sum(
                    estimate_tokens_fast(refs.get(key, "")) for key in batch_keys
                )
                if len(batch.items) >= max_items:
                    break
            if not batch.items:
                # The head item could not fit even alone; split it and retry.
                item = queue.popleft()
                chunks = self._split_item(
                    item,
                    max(1, max_input - base_in - batch_refs_tokens),
                    depth=0,
                )
                queue.extendleft(reversed(chunks))
                continue
            batch.input_tokens = base_in + batch_refs_tokens + sum(
                item.est_input_tokens for item in batch.items
            )
            batch.output_tokens = sum(
                item.est_output_tokens for item in batch.items
            )
            batch.shared_ref_keys = sorted(batch_keys)
            batches.append(batch)
        return batches

    def pack(self, *args: Any, **kwargs: Any) -> list[DynamicBatch]:
        return self.checkout_batches(*args, **kwargs)

    def requeue(
        self,
        pending_queue: deque[DistilledItem],
        batch: DynamicBatch,
    ) -> None:
        """Put a failed batch back at the head, preserving original order."""
        pending_queue.extendleft(reversed(batch.items))

    def coalesce(self, batches: Iterable[DynamicBatch]) -> list[DistilledItem]:
        return [item for batch in batches for item in batch.items]

    # --------------------------------------------------------------- internal
    def _refs_tokens(
        self,
        item: DistilledItem,
        refs: Mapping[str, str],
    ) -> int:
        return sum(
            estimate_tokens_fast(refs.get(key, ""))
            for key in item.shared_ref_keys
        )

    def _split_item(
        self,
        item: DistilledItem,
        capacity: int,
        *,
        depth: int,
    ) -> list[DistilledItem]:
        if capacity <= 0:
            raise ValueError("无法拆分片段：节点容量不足")
        if item.est_input_tokens <= capacity:
            return [item]
        if depth >= self.max_split_depth:
            return self._split_by_characters(item, capacity, depth)
        text = item.text
        chunks = self._paragraph_chunks(text)
        if len(chunks) <= 1:
            chunks = self._line_chunks(text)
        if len(chunks) <= 1:
            return self._split_by_characters(item, capacity, depth)
        result: list[DistilledItem] = []
        current: list[str] = []
        current_tokens = 0
        for chunk in chunks:
            tokens = estimate_tokens_fast(chunk)
            if tokens > capacity:
                if current:
                    joined = "\n".join(current)
                    result.append(item.with_text(joined, f"#{len(result) + 1}"))
                    current = []
                    current_tokens = 0
                result.extend(
                    self._split_item(
                        item.with_text(chunk, f"#{len(result) + 1}"),
                        capacity,
                        depth=depth + 1,
                    )
                )
                continue
            if current and current_tokens + tokens > capacity:
                joined = "\n".join(current)
                result.append(item.with_text(joined, f"#{len(result) + 1}"))
                current = []
                current_tokens = 0
            current.append(chunk)
            current_tokens += tokens
        if current:
            result.append(
                item.with_text("\n".join(current), f"#{len(result) + 1}")
            )
        return result or [item]

    @staticmethod
    def _paragraph_chunks(text: str) -> list[str]:
        chunks = [chunk.strip() for chunk in re.split(r"\n\s*\n+", text) if chunk.strip()]
        return chunks if len(chunks) > 1 else [text]

    @staticmethod
    def _line_chunks(text: str) -> list[str]:
        chunks = [chunk.strip() for chunk in text.splitlines() if chunk.strip()]
        return chunks if len(chunks) > 1 else [text]

    def _split_by_characters(
        self,
        item: DistilledItem,
        capacity: int,
        depth: int,
    ) -> list[DistilledItem]:
        text = item.text
        if not text:
            return [item]
        ratio = max(0.05, min(1.0, capacity / max(1, item.est_input_tokens)))
        target_size = max(1, int(len(text) * ratio * 0.9))

        pieces: list[str] = []
        start = 0
        total_len = len(text)
        while start < total_len:
            if start + target_size >= total_len:
                remaining = text[start:].strip()
                if remaining:
                    pieces.append(remaining)
                break
            candidate = start + target_size
            # Snap cut position to line break if available within range to preserve table rows and sentences
            nl_pos = text.rfind("\n", start, candidate + 1)
            if nl_pos > start:
                end = nl_pos
            else:
                forward_limit = min(total_len, start + int(target_size * 1.25))
                nl_forward = text.find("\n", candidate, forward_limit)
                if nl_forward != -1:
                    end = nl_forward
                else:
                    end = candidate
            piece = text[start:end].strip()
            if piece:
                pieces.append(piece)
            start = end + 1 if end < total_len and text[end:end + 1] == "\n" else end

        result = [
            item.with_text(piece, f"#{index + 1}")
            for index, piece in enumerate(pieces)
            if piece
        ]
        # Character splitting can still overshoot the estimate slightly; do a
        # second pass when a piece is unexpectedly above capacity.
        fixed: list[DistilledItem] = []
        for piece in result:
            if piece.est_input_tokens <= capacity or len(piece.text) <= 1:
                fixed.append(piece)
            else:
                fixed.extend(
                    self._split_by_characters(
                        piece,
                        max(1, capacity),
                        depth + 1,
                    )
                )
        return fixed or [item]


__all__ = [
    "estimate_tokens_fast",
    "DistilledItem",
    "DynamicBatch",
    "DynamicBatchPacker",
]
