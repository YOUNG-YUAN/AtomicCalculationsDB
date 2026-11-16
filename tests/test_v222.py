import json
import sqlite3
from pathlib import Path

import pytest

from db.sqlite_manager import SQLiteManager
from models.atomic_prompts import ExtractionResult, KnowledgeCardDraft
from parser.card_quality import purge_invalid_cards, sanitize_existing_cards
from parser.context_bundler import (
    ContextBundle,
    bundle_hash,
    bundle_hash_from_text,
)
from parser.document_profile import is_manual_document, is_manual_skip_section
from parser.four_channel_radar import FourChannelRadar
from parser.metadata_extractor import parse_zotero_filename, repair_invalid_documents
from parser.pdf_to_markdown import ParsedDocument, Section
from pipeline import Pipeline
from reprocess import ReprocessManager


def _db(tmp_path: Path) -> SQLiteManager:
    return SQLiteManager(tmp_path / "workspace", embedding_signature="test::dim=4")


def test_legacy_database_migrates_before_hash_index(tmp_path: Path):
    """Regression: old DBs lack bundle_hash/is_user_verified before migration."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    schema = (
        Path(__file__).resolve().parents[1] / "app_core" / "db" / "schema_v2.sql"
    ).read_text(encoding="utf-8")
    schema = schema.replace(
        "    is_user_verified        INTEGER NOT NULL DEFAULT 0,\n", ""
    ).replace(
        "    bundle_hash       TEXT NOT NULL DEFAULT '',\n", ""
    )
    # Remove the hash-index comment/enable block; the manager creates it after
    # ALTER TABLE, so the schema script must not reference the new column.
    schema = schema.replace(
        "-- bundle_hash index is created after _ensure_columns() so legacy databases\n"
        "-- are migrated before SQLite evaluates the column reference.\n",
        "",
    )
    connection = sqlite3.connect(workspace / "atomic_meta.sqlite")
    connection.executescript(schema)
    connection.close()

    db = SQLiteManager(workspace, embedding_signature="test::dim=4")
    evidence_columns = {
        row[1] for row in db.conn.execute("PRAGMA table_info(raw_evidence_chunks)")
    }
    card_columns = {
        row[1] for row in db.conn.execute("PRAGMA table_info(knowledge_cards)")
    }
    assert "bundle_hash" in evidence_columns
    assert "is_user_verified" in card_columns
    indexes = {
        row[1] for row in db.conn.execute("PRAGMA index_list(raw_evidence_chunks)")
    }
    assert "idx_evidence_hash" in indexes
    db.close()


def test_zotero_filename_and_metadata_repair(tmp_path: Path):
    pdf = tmp_path / (
        "Froese Fischer 等 - 2019 - GRASP2018—A Fortran 95 version of the "
        "General Relativistic Atomic Structure Package.pdf"
    )
    pytest.importorskip("fitz")
    import pymupdf

    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((60, 100), "C ~ g J ! 5 (", fontsize=16)
    document.save(pdf)
    document.close()

    parsed = parse_zotero_filename(pdf)
    assert parsed.title.startswith("GRASP2018")
    assert parsed.year == 2019
    assert parsed.authors

    db = _db(tmp_path)
    doc_id = db.upsert_document(
        {"title": "untitled", "pub_year": 0, "local_pdf_path": str(pdf)}
    )
    counts = repair_invalid_documents(
        db, tmp_path / "workspace", allow_network=False, include_valid=True
    )
    assert counts["repaired"] == 1
    repaired = db.get_document(doc_id)
    assert repaired["title"].startswith("GRASP2018")
    assert repaired["pub_year"] == 2019
    assert repaired["authors_list"]
    db.close()


def test_manual_detection_probe_skips_software_sections():
    document = ParsedDocument(
        title="GRASP2018—A Fortran 95 version of the package",
        sections=[
            Section(
                "Installation",
                1,
                "Installation",
                paragraphs=["In practice it is advisable to compile with the same compiler."],
            ),
            Section(
                "Worked example: sample run",
                1,
                "Worked example: sample run",
                paragraphs=[
                    "In practice, it is advisable to add correlation orbitals layer by layer "
                    "and hold the spectroscopic orbitals fixed."
                ],
            ),
            Section("References", 1, "References", paragraphs=["Reference list."]),
        ],
    )
    assert is_manual_document(
        title=document.title,
        section_titles=[section.title for section in document.sections],
    )
    assert is_manual_skip_section("2. Compilation")
    result = FourChannelRadar().scan(document, manual=True)
    paths = {hit.section_path for hit in result.hits}
    assert "Installation" not in paths
    assert "References" not in paths
    assert "Worked example: sample run" in paths
    assert any(hit.meta.get("manual_cue") for hit in result.hits)


def test_verified_card_protection_and_bundle_hash(tmp_path: Path):
    db = _db(tmp_path)
    doc_id = db.upsert_document({"title": "Verified card protection test"})
    auto_id = db.upsert_knowledge_card(
        {
            "card_type": "CONC",
            "title_zh": "自动生成卡片",
            "summary_zh": "自动结论",
            "premises_and_conditions": "自动前提",
            "supporting_doc_ids": [doc_id],
        }
    )
    verified_id = db.upsert_knowledge_card(
        {
            "card_type": "STRAT",
            "title_zh": "人工校准卡片",
            "summary_zh": "人工结论",
            "premises_and_conditions": "人工前提",
            "supporting_doc_ids": [doc_id],
            "is_user_verified": 1,
        }
    )
    bundle = ContextBundle(
        doc_id=doc_id,
        section_path="2. Strategy",
        focal_window_markdown="⟪FOCUS⟫ Use active space n=7. ⟪/FOCUS⟫",
        focal_sentence="Use active space n=7.",
    )
    digest = bundle_hash(bundle)
    db.add_evidence(
        verified_id, doc_id, bundle.prompt_markdown(),
        bundle_hash=digest, page_num=4,
    )
    assert db.has_bundle_card(doc_id, digest)
    # Legacy evidence without a hash is backfilled from the exact prompt text.
    legacy_id = db.add_evidence(
        verified_id, doc_id, "legacy evidence text", page_num=5
    )
    db.conn.execute(
        "UPDATE raw_evidence_chunks SET bundle_hash='' WHERE chunk_id=?",
        (legacy_id,),
    )
    db.conn.commit()
    assert db.backfill_bundle_hashes(bundle_hash_from_text) == 1
    assert db.has_bundle_card(
        doc_id, bundle_hash_from_text("legacy evidence text")
    )

    result = db.delete_unverified_cards_for_document(doc_id)
    assert result["deleted"] == [auto_id]
    assert db.get_knowledge_card(auto_id) is None
    assert db.get_knowledge_card(verified_id)["is_user_verified"] == 1
    db.close()


def test_incremental_extraction_skips_already_extracted_bundle(tmp_path: Path):
    db = _db(tmp_path)
    doc_id = db.upsert_document(
        {"title": "Incremental extraction test", "doc_type": "MAN"}
    )
    bundle = ContextBundle(
        doc_id=doc_id,
        section_path="3. Worked example",
        focal_window_markdown="⟪FOCUS⟫ Run rwfnestimate then rmcdhf. ⟪/FOCUS⟫",
        focal_sentence="Run rwfnestimate then rmcdhf.",
    )

    class FakeGateway:
        def __init__(self):
            self.calls = 0

        def set_active_document(self, doc_id):
            pass

        def extract_cards(self, bundle, *, doc_metadata=None, manual_mode=False):
            self.calls += 1
            return ExtractionResult(
                cards=[
                    KnowledgeCardDraft(
                        card_type="CODE",
                        title_zh="GRASP 工作流",
                        summary_zh="按顺序运行 rwfnestimate 与 rmcdhf。",
                        premises_and_conditions="适用于 GRASP 手册。",
                        code_snippet="rwfnestimate\nrmcdhf",
                    )
                ]
            )

    gateway = FakeGateway()
    pipeline = Pipeline(db, tmp_path / "workspace", gateway=gateway)
    first = pipeline.extract_bundles(doc_id, [bundle], force=False)
    assert first["extracted"] == 1
    assert gateway.calls == 1
    second = pipeline.extract_bundles(doc_id, [bundle], force=False)
    assert second["extracted"] == 0
    assert second["skipped_existing"] == 1
    assert gateway.calls == 1
    db.close()


def test_force_reprocess_preserves_verified_cards(tmp_path: Path):
    db = _db(tmp_path)
    doc_id = db.upsert_document({"title": "Force reprocess test"})
    db.upsert_knowledge_card(
        {
            "card_type": "CONC",
            "title_zh": "自动卡片",
            "summary_zh": "旧结论",
            "premises_and_conditions": "旧前提",
            "supporting_doc_ids": [doc_id],
        }
    )
    verified_id = db.upsert_knowledge_card(
        {
            "card_type": "CONC",
            "title_zh": "人工卡片",
            "summary_zh": "人工结论",
            "premises_and_conditions": "人工前提",
            "supporting_doc_ids": [doc_id],
            "is_user_verified": 1,
        }
    )
    removed = db.delete_unverified_cards_for_document(doc_id)
    assert len(removed["deleted"]) == 1
    assert db.get_knowledge_card(verified_id)["is_user_verified"] == 1
    db.close()


def test_local_reprocess_cleans_formula_and_truncated_cards(tmp_path: Path):
    db = _db(tmp_path)
    good_id = db.upsert_knowledge_card(
        {
            "card_type": "MODEL",
            "title_zh": "旧公式卡片",
            "summary_zh": "旧结论。",
            "premises_and_conditions": "旧前提。",
            "formulas_latex": "vuuut1 H = \\sum_i h_D(i)",
        }
    )
    bad_id = db.upsert_knowledge_card(
        {
            "card_type": "CONC",
            "title_zh": "（证据截断）",
            "summary_zh": "无法可靠重建具体计算策略",
            "premises_and_conditions": "未知",
        }
    )

    class FakePipeline:
        consensus = None

    manager = ReprocessManager(db, tmp_path / "workspace", FakePipeline())
    events: list[tuple[str, float]] = []
    result = manager.local_clean(progress=lambda label, value: events.append((label, value)))
    assert result["purged"]["deleted"] == 1
    assert result["formulas"]["updated"] == 1
    assert db.get_knowledge_card(bad_id) is None
    assert "vuuut" not in db.get_knowledge_card(good_id)["formulas_latex"]
    assert events[0][1] <= 0.03
    assert events[-1] == ("✅ 本地清洗完成", 1.0)
    assert all(left[1] <= right[1] for left, right in zip(events, events[1:]))
    assert any(label.startswith("阶段 2/5") for label, _value in events)
    assert any(label.startswith("阶段 3/5") for label, _value in events)
    db.close()
