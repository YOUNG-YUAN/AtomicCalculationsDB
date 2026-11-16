"""Schema v3.1: non-destructive relations, parse quality, vector retry."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from db.sqlite_manager import SQLiteManager
from models.atomic_prompts import ExtractionResult, KnowledgeCardDraft
from parser.context_bundler import ContextBundle
from pipeline import Pipeline


def _db(tmp_path: Path) -> SQLiteManager:
    return SQLiteManager(tmp_path / "workspace", embedding_signature="test::dim=4")


def _draft() -> KnowledgeCardDraft:
    return KnowledgeCardDraft(
        card_type="STRAT",
        title_zh="测试策略",
        summary_zh="扩大活性空间到 n=8 后收敛。",
        premises_and_conditions="基态，偶宇称。",
    )


def _bundle(doc_id: str, section: str, focus: str) -> ContextBundle:
    return ContextBundle(
        doc_id=doc_id,
        section_path=section,
        focal_window_markdown=f"⟪FOCUS⟫ {focus} ⟪/FOCUS⟫",
        focal_sentence=focus,
    )


class _FakeGateway:
    def extract_cards(self, bundle, *, doc_metadata=None, manual_mode=False):
        return ExtractionResult(cards=[_draft()])


def test_schema_v31_has_relations_and_parse_error_code(tmp_path: Path):
    db = _db(tmp_path)
    try:
        assert db.get_meta("schema_version") == "3.2"
        assert db.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='card_relations'"
        ).fetchone()
        columns = {
            row["name"] for row in db.conn.execute("PRAGMA table_info(documents)")
        }
        assert "parse_error_code" in columns
    finally:
        db.close()


def test_reopen_v30_database_adds_v31_objects(tmp_path: Path):
    db = _db(tmp_path)
    try:
        db.conn.execute("DROP TABLE card_relations")
        db.conn.execute("ALTER TABLE documents DROP COLUMN parse_error_code")
        db.set_meta("schema_version", "3.0")
        db.conn.commit()
    finally:
        db.close()

    reopened = _db(tmp_path)
    try:
        assert reopened.get_meta("schema_version") == "3.2"
        assert reopened.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='card_relations'"
        ).fetchone()
        columns = {
            row["name"] for row in reopened.conn.execute("PRAGMA table_info(documents)")
        }
        assert "parse_error_code" in columns
    finally:
        reopened.close()


def test_card_relations_are_bidirectional_in_detail(tmp_path: Path):
    db = _db(tmp_path)
    try:
        a = db.upsert_knowledge_card(
            {
                "card_type": "STRAT",
                "title_zh": "策略 A",
                "summary_zh": "结论 A",
                "premises_and_conditions": "前提 A",
            }
        )
        b = db.upsert_knowledge_card(
            {
                "card_type": "STRAT",
                "title_zh": "策略 B",
                "summary_zh": "结论 B",
                "premises_and_conditions": "前提 B",
            }
        )
        relation_id = db.add_card_relation(
            a, b, "SAME_CLAIM", confidence=0.91, reason="测试关系"
        )
        assert relation_id.startswith("REL-")

        detail_a = db.get_knowledge_card(a)
        detail_b = db.get_knowledge_card(b)
        assert detail_a["relations"][0]["other_knw_id"] == b
        assert detail_b["relations"][0]["other_knw_id"] == a
        assert detail_a["relations"][0]["relation_type"] == "SAME_CLAIM"
        assert detail_a["relations"][0]["confidence"] == 0.91
    finally:
        db.close()


def test_duplicate_extraction_keeps_both_cards_and_links_them(tmp_path: Path):
    db = _db(tmp_path)
    try:
        doc_a = db.upsert_document({"title": "Paper A", "pub_year": 2024})
        doc_b = db.upsert_document({"title": "Paper B", "pub_year": 2024})
        pipeline = Pipeline(db, tmp_path / "workspace", gateway=_FakeGateway())

        pipeline.extract_bundles(
            doc_a, [_bundle(doc_a, "1. A", "扩大活性空间到 n=8 后收敛。")]
        )
        pipeline.extract_bundles(
            doc_b, [_bundle(doc_b, "1. B", "扩大活性空间到 n=8 后收敛。")]
        )

        cards = db.list_knowledge_cards(limit=10)
        assert len(cards) == 2
        for card in cards:
            detail = db.get_knowledge_card(card["knw_id"])
            assert len(detail["evidence"]) == 1
            assert len(detail["relations"]) == 1
            assert detail["relations"][0]["relation_type"] == "SAME_CLAIM"
    finally:
        db.close()


def test_failed_vector_refresh_queues_a_delayed_retry(tmp_path: Path):
    db = _db(tmp_path)
    try:
        card_id = db.upsert_knowledge_card(
            {
                "card_type": "CONC",
                "title_zh": "向量重试",
                "summary_zh": "用于测试向量重试。",
                "premises_and_conditions": "测试。",
            }
        )
        db.mark_cards_vector_pending([card_id])
        pipeline = Pipeline(db, tmp_path / "workspace", gateway=None)
        pipeline._vectorize = lambda _knw_id: False  # type: ignore[method-assign]

        result = pipeline.handle_vectorize(
            {
                "task_id": 1,
                "attempts": 1,
                "payload_json": json.dumps({"knw_id": card_id}),
            }
        )
        assert result is True
        assert db.get_knowledge_card(card_id)["needs_vector_update"] == 1
        tasks = db.list_tasks(status="PENDING")
        assert any(
            task["task_type"] == "VECTORIZE"
            and card_id in task["payload_json"]
            and task["scheduled_for"]
            for task in tasks
        )
    finally:
        db.close()


def test_claim_next_task_respects_scheduled_for(tmp_path: Path):
    db = _db(tmp_path)
    try:
        future = (
            datetime.now().astimezone() + timedelta(minutes=10)
        ).isoformat(timespec="seconds")
        db.add_task("VECTORIZE", payload={"knw_id": "KNW-STRAT-2024-ABCDEF"}, scheduled_for=future)
        assert db.claim_next_task() is None

        db.add_task("VECTORIZE", payload={"knw_id": "KNW-STRAT-2024-ABCDEG"})
        claimed = db.claim_next_task()
        assert claimed is not None
        assert claimed["task_type"] == "VECTORIZE"
    finally:
        db.close()
