from collections import deque
from types import SimpleNamespace

from parser.batch_packer import (
    DistilledItem,
    DynamicBatchPacker,
    estimate_tokens_fast,
)


def _node(max_input=5000, max_output=4000, max_items=10):
    return SimpleNamespace(
        max_input_tokens=max_input,
        max_output_tokens=max_output,
        max_items_per_batch=max_items,
    )


def _items(count, words=577):
    return [
        DistilledItem(
            item_id=f"item-{index}",
            text=f"strategy {index} " + ("alpha " * words),
        )
        for index in range(count)
    ]


def test_fast_token_estimator_is_deterministic():
    text = r"MCDHF active space n=7; 中文结论 with \LaTeX{} and $E=mc^2$."
    first = estimate_tokens_fast(text)
    second = estimate_tokens_fast(text)
    assert first == second
    assert first > 10


def test_packer_splits_15k_items_for_5k_node_and_coalesces_large():
    items = _items(20)
    packer = DynamicBatchPacker(system_tokens=0, global_premise_tokens=0)
    queue = deque(items)
    small_batches = packer.checkout_batches(queue, _node(5000, 4000, 10))
    assert 3 <= len(small_batches) <= 4
    assert sum(batch.item_count for batch in small_batches) == 20
    assert all(batch.input_tokens <= 5000 for batch in small_batches)

    # A 5k node could not finish; return every item and retry on a 64k node.
    requeued = deque()
    for batch in small_batches:
        packer.requeue(requeued, batch)
    large_batches = packer.checkout_batches(
        requeued, _node(64000, 16000, 40)
    )
    assert len(large_batches) == 1
    assert large_batches[0].item_count == 20
    assert large_batches[0].input_tokens <= 64000


def test_oversized_item_is_split_at_boundaries():
    huge = DistilledItem(
        item_id="huge",
        text="\n\n".join(("alpha " * 700) for _ in range(8)),
    )
    assert huge.est_input_tokens > 5000
    packer = DynamicBatchPacker(system_tokens=0, global_premise_tokens=0)
    queue = deque([huge])
    batches = packer.checkout_batches(queue, _node(5000, 4000, 4))
    assert len(batches) >= 2
    assert sum(batch.item_count for batch in batches) >= 2
    assert all(batch.input_tokens <= 5000 for batch in batches)


def test_endpoint_pool_routes_day_and_night_and_skips_inactive_nodes():
    from models.endpoint_pool import (
        EndpointNode,
        EndpointPool,
        ModelStrategy,
        RoleBinding,
        TimeSlotRule,
    )
    from datetime import time

    config = {"endpoint_pool": {"nodes": [], "strategies": [], "role_bindings": {}}}
    pool = EndpointPool(config)
    day = EndpointNode(
        node_id="day",
        name="白天节点",
        capabilities=["TEXT_LLM"],
        base_url="https://day.example/v1",
        model="day-model",
        time_restriction_enabled=True,
        allowed_start_time="08:00",
        allowed_end_time="18:00",
    )
    night = EndpointNode(
        node_id="night",
        name="夜间节点",
        capabilities=["TEXT_LLM"],
        base_url="https://night.example/v1",
        model="night-model",
        time_restriction_enabled=True,
        allowed_start_time="18:00",
        allowed_end_time="08:00",
    )
    pool.nodes = {"day": day, "night": night}
    pool.strategies = {
        "day-strategy": ModelStrategy("day-strategy", "白天", ["day"]),
        "night-strategy": ModelStrategy("night-strategy", "夜间", ["night"]),
    }
    pool.bindings = {
        "fast_llm": RoleBinding(
            default_strategy_id="day-strategy",
            time_slot_rules=[
                TimeSlotRule("夜间", "18:00", "08:00", "night-strategy")
            ],
        )
    }
    assert pool.node_for_role("fast_llm", time(14, 0)).node_id == "day"
    assert pool.node_for_role("fast_llm", time(22, 0)).node_id == "night"
    assert [node.node_id for node in pool.chain_for_role("fast_llm", time(14, 0))] == ["day"]
    assert [node.node_id for node in pool.chain_for_role("fast_llm", time(22, 0))] == ["night"]
    pool.save()
    assert config["endpoint_pool"]["nodes"]


def test_relay_requeues_5k_timeout_and_coalesces_for_64k_node():
    from models.endpoint_pool import EndpointNode, EndpointPool, ModelStrategy
    from models.relay_executor import BatchOutcome, QueueTimeout, RelayExecutor

    config = {"endpoint_pool": {"nodes": [], "strategies": [], "role_bindings": {}}}
    pool = EndpointPool(config)
    small = EndpointNode(
        node_id="small",
        name="Web 5k",
        capabilities=["TEXT_LLM"],
        base_url="https://small.example/v1",
        model="web",
        max_input_tokens=5000,
        max_output_tokens=4000,
        max_items_per_batch=10,
    )
    large = EndpointNode(
        node_id="large",
        name="API 64k",
        capabilities=["TEXT_LLM"],
        base_url="https://large.example/v1",
        model="api",
        max_input_tokens=64000,
        max_output_tokens=16000,
        max_items_per_batch=40,
    )
    pool.nodes = {"small": small, "large": large}
    pool.strategies = {
        "s": ModelStrategy("s", "接力", ["small", "large"], enable_loop=False)
    }
    pool.bindings["fast_llm"].default_strategy_id = "s"
    seen = []

    def call(node, batch):
        seen.append((node.node_id, batch.item_count))
        if node.node_id == "small":
            raise QueueTimeout("queued too long")
        return BatchOutcome(success_items=list(batch.items), payloads=["ok"])

    executor = RelayExecutor(
        pool,
        DynamicBatchPacker(system_tokens=0, global_premise_tokens=0),
        sleep=lambda _seconds: None,
    )
    result = executor.run(_items(20), "fast_llm", call)
    assert result.completed == ["ok"]
    assert result.failed_items == []
    assert seen[0][0] == "small"
    assert seen[-1] == ("large", 20)
    assert result.attempts[0]["status"] == "queue_timeout"


def test_queue_poller_distinguishes_queued_timeout_and_processing():
    from models.endpoint_pool import EndpointNode
    from models.relay_executor import QueuePoller, QueueTimeout

    node = EndpointNode(
        node_id="q",
        name="队列节点",
        node_type="QUEUE_POLLING_API",
        capabilities=["TEXT_LLM"],
        poll_interval_sec=20,
        max_queue_wait_sec=40,
    )
    state = {"now": 0.0, "index": 0}
    statuses = ["queued", "queued", "processing", "processing", "done"]

    def sleep(seconds):
        state["now"] += seconds

    poller = QueuePoller(sleep=sleep, clock=lambda: state["now"])

    def get_status():
        index = min(state["index"], len(statuses) - 1)
        state["index"] += 1
        return {"status": statuses[index]}

    result = poller.wait(node, get_status)
    assert result["status"] == "done"
    assert state["now"] >= 60  # processing removed the 40s queue deadline

    state = {"now": 0.0}
    with __import__("pytest").raises(QueueTimeout):
        poller.wait(node, lambda: {"status": "queued"})


def test_relay_loop_cooldown_returns_to_first_node():
    from models.endpoint_pool import EndpointNode, EndpointPool, ModelStrategy
    from models.relay_executor import BatchOutcome, NodeBusy, RelayExecutor

    config = {"endpoint_pool": {"nodes": [], "strategies": [], "role_bindings": {}}}
    pool = EndpointPool(config)
    for node_id in ("a", "b"):
        pool.nodes[node_id] = EndpointNode(
            node_id=node_id,
            name=node_id,
            capabilities=["TEXT_LLM"],
            base_url=f"https://{node_id}.example/v1",
            model="m",
            max_input_tokens=64000,
            max_output_tokens=16000,
            max_items_per_batch=40,
        )
    pool.strategies = {
        "loop": ModelStrategy(
            "loop", "循环", ["a", "b"], enable_loop=True,
            loop_cooldown_sec=0, max_loops=1,
        )
    }
    pool.bindings["fast_llm"].default_strategy_id = "loop"
    calls = []

    def call(node, batch):
        calls.append(node.node_id)
        if len(calls) <= 2:
            raise NodeBusy("busy")
        return BatchOutcome(success_items=list(batch.items), payloads=["done"])

    executor = RelayExecutor(
        pool,
        DynamicBatchPacker(system_tokens=0, global_premise_tokens=0),
        sleep=lambda _seconds: None,
    )
    result = executor.run(_items(1), "fast_llm", call)
    assert calls == ["a", "b", "a"]
    assert result.completed == ["done"]


def test_clipboard_packet_build_and_reply_parse(tmp_path):
    import json
    from types import SimpleNamespace
    from db.sqlite_manager import SQLiteManager
    from models.endpoint_pool import EndpointNode, EndpointPool
    from models.web_ai_bridge import build_document_packet
    from parser.context_bundler import ContextBundle

    db = SQLiteManager(tmp_path / "workspace", embedding_signature="test::dim=4")
    storage = tmp_path / "workspace" / "storage" / "DOC-PAP-CLIP"
    storage.mkdir(parents=True)
    pdf = storage / "paper.pdf"
    pdf.write_bytes(b"%PDF fake")
    doc_id = db.upsert_document(
        {"title": "Clipboard document", "local_pdf_path": str(pdf)}
    )
    bundle = ContextBundle(
        doc_id=doc_id,
        section_path="1. Worked example",
        focal_window_markdown="⟪FOCUS⟫ Run rwfnestimate then rmcdhf. ⟪/FOCUS⟫",
        focal_sentence="Run rwfnestimate then rmcdhf.",
    )
    (storage / "bundles.json").write_text(
        json.dumps([bundle.model_dump()], ensure_ascii=False),
        encoding="utf-8",
    )
    pool = EndpointPool({"endpoint_pool": {}})
    pool.nodes["clip"] = EndpointNode(
        node_id="clip",
        name="剪贴板",
        node_type="SEMI_AUTO_CLIPBOARD",
        capabilities=["TEXT_LLM"],
        max_input_tokens=5000,
        max_output_tokens=4000,
        max_items_per_batch=6,
    )
    pipeline = SimpleNamespace(
        gateway=SimpleNamespace(pool=pool),
        db=db,
    )
    document = db.get_document(doc_id)
    service, packet = build_document_packet(
        db, tmp_path / "workspace", document, pipeline
    )
    assert packet.batch.item_count == 1
    assert "rwfnestimate" in packet.prompt
    reply = {
        "items": [
            {
                "item_id": packet.batch.items[0].item_id,
                "result": {
                    "cards": [
                        {
                            "card_type": "CODE",
                            "title_zh": "GRASP 工作流",
                            "summary_zh": "按顺序运行程序。",
                            "premises_and_conditions": "适用于手册示例。",
                        }
                    ]
                },
            }
        ]
    }
    results = service.parse_reply(json.dumps(reply, ensure_ascii=False))
    assert len(results) == 1
    db.close()


def test_builtin_local_nodes_default_and_cannot_be_deleted(tmp_path):
    from models.endpoint_pool import EndpointPool, ProtectedEndpointError

    pool = EndpointPool({"endpoint_pool": {}})
    ids = set(pool.nodes)
    assert {
        "builtin-local-bge-m3",
        "builtin-local-reranker",
        "builtin-local-minilm",
        "builtin-local-nli",
    } <= ids
    for role in ("embedding", "reranker"):
        strategy = pool.strategies[pool.bindings[role].default_strategy_id]
        assert strategy.relay_chain
        assert strategy.relay_chain[0].startswith("builtin-local-")
    with __import__("pytest").raises(ProtectedEndpointError):
        pool.remove_node("builtin-local-bge-m3")
    with __import__("pytest").raises(ProtectedEndpointError):
        pool.remove_node("builtin-local-reranker")


def test_gateway_uses_builtin_embedding_node_signature(tmp_path):
    from config import AppConfig
    from models.gateway import ModelGateway

    gateway = ModelGateway(AppConfig({}), tmp_path)
    assert gateway.embedding.node.node_id == "builtin-local-bge-m3"
    assert gateway.embedding.signature == "BAAI/bge-m3::dim=1024"
    assert gateway.embedding.available is False  # BGE-M3 file is not in the temp workspace
    assert gateway.reranker.node.node_id == "builtin-local-reranker"


def test_eta_tracker_formats_remaining_and_finishes(monkeypatch):
    import ui.progress_eta as progress_eta

    clock = [100.0]
    monkeypatch.setattr(progress_eta.time, "monotonic", lambda: clock[0])
    tracker = progress_eta.EtaTracker()
    tracker.reset()

    clock[0] = 101.0
    elapsed, remaining = tracker.update(0.1)
    assert elapsed == 1.0
    assert remaining is not None and remaining > 0
    assert "预计剩余" in tracker.summary(0.1)

    clock[0] = 106.0
    elapsed, remaining = tracker.update(0.5)
    assert elapsed == 6.0
    assert remaining is not None and 0 < remaining < 60

    clock[0] = 110.0
    elapsed, remaining = tracker.update(1.0)
    assert elapsed == 10.0
    assert remaining == 0
    assert "已完成" in tracker.summary(1.0)
    assert progress_eta.format_duration(65) == "1 分 05 秒"


def test_conflict_engine_batches_nli_pairs():
    from parser.consensus_conflict_engine import ConsensusConflictEngine

    class FakeRegistry:
        def is_present(self, name):
            return name == "conflict_nli"

        def nli_batch(self, pairs):
            assert len(pairs) == 2
            return [{"contradiction": 0.91}, {"contradiction": 0.08}]

    engine = ConsensusConflictEngine(registry=FakeRegistry())
    verdicts = engine.detect_conflict_batch([("a", "b"), ("c", "d")])
    assert [verdict.conflict for verdict in verdicts] == [True, False]
    assert all(verdict.method == "nli" for verdict in verdicts)


def test_eta_stage_rates_ignore_model_load_and_bookkeeping(monkeypatch):
    import ui.progress_eta as progress_eta

    clock = [0.0]
    monkeypatch.setattr(progress_eta.time, "monotonic", lambda: clock[0])
    tracker = progress_eta.EtaTracker()
    tracker.reset()

    tracker.update(0.47, "阶段 4/5 · MiniLM 语义编码 0/3648")
    clock[0] = 15.0
    tracker.update(0.485, "阶段 4/5 · MiniLM 语义编码 512/3648")
    clock[0] = 30.0
    _elapsed, remaining = tracker.update(
        0.50, "阶段 4/5 · MiniLM 语义编码 1024/3648"
    )
    assert remaining is not None
    assert 250 < remaining < 450

    tracker.reset()
    clock[0] = 50.0
    tracker.update(0.82, "阶段 5/5 · 加载 NLI 争议模型")
    clock[0] = 70.0
    tracker.update(0.821, "阶段 5/5 · 争议校验 1/200 对")
    clock[0] = 75.0
    _elapsed, remaining = tracker.update(
        0.83, "阶段 5/5 · 争议校验 20/200 对"
    )
    assert remaining is not None
    assert 50 < remaining < 150


def test_cluster_uses_vectorized_batches_without_pairwise_loop():
    from parser.consensus_conflict_engine import ConsensusConflictEngine

    class FakeRegistry:
        def is_present(self, name):
            return name == "mini_cluster_embed"

        def embed_minilm(self, texts, progress=None):
            vectors = []
            for index, _text in enumerate(texts):
                group = index // 50
                vector = [0.0] * 4
                vector[group % 4] = 1.0
                vectors.append(vector)
            return vectors

    texts = [f"card {index}" for index in range(200)]
    groups = ConsensusConflictEngine(registry=FakeRegistry()).cluster(texts)
    assert len(groups) == 4
    assert sum(len(group) for group in groups) == 200
