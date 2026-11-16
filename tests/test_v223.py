import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from db.sqlite_manager import SQLiteManager
from models.atomic_prompts import ExtractionResult, KnowledgeCardDraft
from parser.context_bundler import ContextBundle, bundle_hash
from parser.pdf_to_markdown import PDFParser
from pipeline import Pipeline
from reprocess import ReprocessManager


def _db(tmp_path: Path) -> SQLiteManager:
    return SQLiteManager(tmp_path / "workspace", embedding_signature="test::dim=4")


def _bundle(doc_id: str, section: str, focus: str) -> ContextBundle:
    return ContextBundle(
        doc_id=doc_id,
        section_path=section,
        focal_window_markdown=f"⟪FOCUS⟫ {focus} ⟪/FOCUS⟫",
        focal_sentence=focus,
    )


def _draft() -> KnowledgeCardDraft:
    return KnowledgeCardDraft(
        card_type="STRAT",
        title_zh="测试策略",
        summary_zh="测试结论。",
        premises_and_conditions="测试前提。",
    )


def test_sparse_pdf_is_marked_failed_before_extraction(tmp_path: Path):
    pymupdf = pytest.importorskip("pymupdf")

    pdf = tmp_path / "scanned.pdf"
    document = pymupdf.open()
    for _ in range(3):
        page = document.new_page()
        page.insert_text((72, 72), "1984A&A...135..313L", fontsize=10)
    document.save(pdf)
    document.close()

    db = _db(tmp_path)
    doc_id = db.upsert_document(
        {
            "title": "Scanned paper",
            "doc_type": "PAP",
            "local_pdf_path": str(pdf),
        }
    )
    pipeline = Pipeline(
        db, tmp_path / "workspace", parser=PDFParser(allow_network=False)
    )

    assert pipeline.parse_document(doc_id) is None
    saved = db.get_document(doc_id)
    assert saved["parse_status"] == "FAILED"
    assert saved["parse_error_code"] == "NEEDS_OCR"
    assert "文本层过少" in str(saved["error_log"])
    assert "扫描件" in str(saved["error_log"])
    db.close()


def test_zero_card_extraction_does_not_stay_in_parsing(tmp_path: Path):
    db = _db(tmp_path)
    doc_id = db.upsert_document({"title": "Zero card test"})
    bundle = _bundle(doc_id, "1. Introduction", "No extractable content.")

    class FakeGateway:
        def extract_cards(self, bundle, *, doc_metadata=None, manual_mode=False):
            return ExtractionResult(skip=True, skip_reason="证据截断")

    pipeline = Pipeline(db, tmp_path / "workspace", gateway=FakeGateway())
    result = pipeline.extract_bundles(doc_id, [bundle])

    assert result["cards"] == 0
    assert result["cancelled"] is False
    saved = db.get_document(doc_id)
    assert saved["parse_status"] == "FAILED"
    assert "未生成知识卡" in str(saved["error_log"])
    db.close()


def test_cancel_stops_between_bundles_and_keeps_partial_cards(tmp_path: Path):
    db = _db(tmp_path)
    doc_id = db.upsert_document({"title": "Cancel test"})
    bundles = [
        _bundle(doc_id, "1. First", "Use active space n=7."),
        _bundle(doc_id, "2. Second", "Use active space n=8."),
    ]

    class FakeGateway:
        def __init__(self):
            self.calls = 0

        def extract_cards(self, bundle, *, doc_metadata=None, manual_mode=False):
            self.calls += 1
            return ExtractionResult(cards=[_draft()])

    gateway = FakeGateway()
    pipeline = Pipeline(db, tmp_path / "workspace", gateway=gateway)
    result = pipeline.extract_bundles(
        doc_id,
        bundles,
        should_stop=lambda: gateway.calls >= 1,
    )

    assert gateway.calls == 1
    assert result["cancelled"] is True
    assert result["cards"] == 1
    saved = db.get_document(doc_id)
    assert saved["parse_status"] == "LIGHT_PARSED"
    assert "已取消" in str(saved["error_log"])
    db.close()


def test_incremental_estimate_only_counts_missing_bundles(tmp_path: Path):
    db = _db(tmp_path)
    storage = tmp_path / "workspace" / "storage" / "DOC-PAP-0000-CACHE"
    storage.mkdir(parents=True)
    pdf = storage / "paper.pdf"
    pdf.write_bytes(b"%PDF fake")
    doc_id = db.upsert_document(
        {
            "title": "Incremental estimate test",
            "local_pdf_path": str(pdf),
        }
    )
    first = _bundle(doc_id, "1. First", "First cached bundle with a long sentence.")
    second = _bundle(
        doc_id,
        "2. Second",
        "Second missing bundle with another long sentence that needs an LLM call.",
    )
    (storage / "bundles.json").write_text(
        f"[{first.model_dump_json()},{second.model_dump_json()}]",
        encoding="utf-8",
    )
    card_id = db.upsert_knowledge_card(
        {
            "card_type": "STRAT",
            "title_zh": "缓存卡片",
            "summary_zh": "缓存结论。",
            "premises_and_conditions": "缓存前提。",
            "supporting_doc_ids": [doc_id],
        }
    )
    db.add_evidence(
        card_id,
        doc_id,
        first.prompt_markdown(),
        bundle_hash=bundle_hash(first),
    )

    manager = ReprocessManager(db, tmp_path / "workspace", SimpleNamespace())
    incremental = manager.estimate_incremental_tokens([doc_id])
    force = manager.estimate_force_tokens([doc_id])
    assert 0 < incremental < force
    db.close()


def test_manual_filter_skips_installation_toc_and_references(tmp_path: Path):
    db = _db(tmp_path)
    doc_id = db.upsert_document(
        {"title": "GRASP2018 practical guide", "doc_type": "MAN"}
    )
    bundles = [
        _bundle(doc_id, "Installation > 2.1 Compiler", "Install with gfortran."),
        _bundle(
            doc_id,
            "Contents",
            "I Overview of GRASP2018 11 .......... 4.1 Generating lists of CSFs 31 ........",
        ),
        _bundle(doc_id, "References", "Froese Fischer et al. 2018."),
        _bundle(
            doc_id,
            "7.1 FIRST EXAMPLE",
            "Run rwfnestimate, then rmcdhf, then rci to converge the active space.",
        ),
    ]

    class FakeGateway:
        def __init__(self):
            self.calls = 0

        def extract_cards(self, bundle, *, doc_metadata=None, manual_mode=False):
            self.calls += 1
            return ExtractionResult(
                cards=[
                    KnowledgeCardDraft(
                        card_type="CODE",
                        title_zh=f"工作流 {bundle.section_path}",
                        summary_zh="按顺序运行 GRASP 程序。",
                        premises_and_conditions="适用于 GRASP 手册示例。",
                    )
                ]
            )

    gateway = FakeGateway()
    pipeline = Pipeline(
        db,
        tmp_path / "workspace",
        config={"extraction": {"concurrency": 1, "skip_low_value_manual": True}},
        gateway=gateway,
    )
    result = pipeline.extract_bundles(doc_id, bundles)

    assert gateway.calls == 1
    assert result["skipped_low_value"] == 3
    assert result["cards"] == 1
    db.close()


def test_parallel_extraction_runs_multiple_calls(tmp_path: Path):
    db = _db(tmp_path)
    doc_id = db.upsert_document({"title": "Parallel extraction test"})
    bundles = [
        _bundle(doc_id, f"Section {index}", f"Unique strategy number {index}.")
        for index in range(6)
    ]

    class FakeGateway:
        def __init__(self):
            self.lock = threading.Lock()
            self.active = 0
            self.max_active = 0

        def extract_cards(self, bundle, *, doc_metadata=None, manual_mode=False):
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            time.sleep(0.05)
            with self.lock:
                self.active -= 1
            return ExtractionResult(
                cards=[
                    KnowledgeCardDraft(
                        card_type="STRAT",
                        title_zh=f"策略 {bundle.section_path}",
                        summary_zh=f"结论 {bundle.focal_sentence}",
                        premises_and_conditions="并行测试前提。",
                    )
                ]
            )

    gateway = FakeGateway()
    pipeline = Pipeline(
        db,
        tmp_path / "workspace",
        config={"extraction": {"concurrency": 3, "skip_low_value_manual": False}},
        gateway=gateway,
    )
    result = pipeline.extract_bundles(doc_id, bundles)

    assert result["extracted"] == 6
    assert gateway.max_active >= 2
    db.close()


def test_large_duplicate_group_uses_anchor_not_all_pairs(tmp_path: Path):
    db = _db(tmp_path)
    for index in range(10):
        db.upsert_knowledge_card(
            {
                "card_type": "MODEL",
                "title_zh": f"重复卡片 {index}",
                "summary_zh": "相同的计算结论。",
                "premises_and_conditions": "相同的前提。",
                "search_payload": "相同的检索文本",
            }
        )

    class FakeEngine:
        def __init__(self):
            self.conflict_calls = 0

        def cluster(self, texts, progress=None):
            if progress:
                progress("fake cluster", 1.0)
            return [list(range(len(texts)))]

        def detect_conflict(self, text_a, text_b):
            self.conflict_calls += 1
            return SimpleNamespace(conflict=False)

    engine = FakeEngine()
    pipeline = SimpleNamespace(consensus=engine, gateway=None)
    manager = ReprocessManager(db, tmp_path / "workspace", pipeline)
    events: list[tuple[str, float]] = []
    result = manager._recluster_local(
        progress=lambda label, value: events.append((label, value))
    )
    assert result["checked"] == 10
    assert result["merged"] == 9
    assert engine.conflict_calls == 9  # anchor comparison, not 10*9/2
    assert db.count_knowledge_cards() == 1
    assert any("争议校验" in label and "9/9" in label for label, _ in events)
    assert events[-1][1] == 0.99
    db.close()
