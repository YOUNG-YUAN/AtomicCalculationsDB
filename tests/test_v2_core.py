import json
import sqlite3
from pathlib import Path

import pytest

from db.id_generator import (
    classify_id,
    extract_ids,
    generate_doc_id,
    generate_knw_id,
    generate_keyword_id,
    generate_master_id,
    normalize_doi,
)
from db.qdrant_manager import QdrantManager
from db.sqlite_manager import EmbeddingSignatureMismatch, SQLiteManager


# --------------------------------------------------------------------- IDs
def test_doc_id_is_deterministic():
    a = generate_doc_id("PAP", 2024, doi="https://doi.org/10.1234/ABC.001")
    b = generate_doc_id("PAP", 2024, doi="doi:10.1234/abc.001")
    assert a == b
    assert a.startswith("DOC-PAP-2024-")
    assert classify_id(a)["family"] == "document"
    assert normalize_doi("https://doi.org/10.1234/ABC.001") == "10.1234/abc.001"


def test_knw_master_keyword_ids():
    knw = generate_knw_id("STRAT", 2024, title="Fe XVII", summary="MCDHF", supporting_doc_ids=["DOC-PAP-2024-ABCDEF"])
    assert knw.startswith("KNW-STRAT-2024-")
    assert generate_master_id("Ne-like", "MCDHF+RCI") == "MASTER-NELIKE-MCDHFRCI"
    assert generate_keyword_id("Core-Valence Correlation") == generate_keyword_id("core-valence correlation")
    text = f"See {knw}, MASTER-NELIKE-MCDHF and KW-ABCDEF"
    assert set(extract_ids(text)) >= {knw, "MASTER-NELIKE-MCDHF", "KW-ABCDEF"}


# ---------------------------------------------------------------- SQLite V2
@pytest.fixture()
def manager(tmp_path: Path):
    db = SQLiteManager(tmp_path / "workspace", embedding_signature="test::dim=8")
    yield db
    db.close()


def _document(manager: SQLiteManager):
    return manager.upsert_document(
        {
            "doc_type": "PAP",
            "title": "Relativistic MCDHF calculations for Fe XVII",
            "doi": "10.1234/acdb.v2.001",
            "pub_year": 2024,
            "authors": ["A. Author"],
            "journal": "Physical Review A",
            "abstract": "MCDHF and RCI with Breit interaction",
            "authority_weight": 1.0,
        }
    )


def _card(manager: SQLiteManager, doc_id: str, **overrides):
    payload = {
        "card_level": "L2",
        "card_type": "STRAT",
        "title_zh": "Fe XVII 的 MCDHF 分层策略",
        "title_en": "Fe XVII MCDHF strategy",
        "summary_zh": "使用活性空间扩展和芯价关联处理 Fe XVII",
        "premises_and_conditions": "适用于 Ne-like 高电荷态离子",
        "method": "MCDHF+RCI",
        "code_name": "GRASP2018",
        "element": "Fe",
        "z_num": 26,
        "isoelectronic": "Ne-like",
        "physical_process": "E1_transition",
        "conclusion_flags": 37,
        "supporting_doc_ids": [doc_id],
        "structured_details_json": {"active_space": "n=7", "core": "2s2 2p6"},
        "formulas_latex": "H = H_D + B_{ij}",
        "code_snippet": "rmcdhf < input",
    }
    payload.update(overrides)
    return manager.upsert_knowledge_card(payload)


def test_schema_and_system_meta(manager: SQLiteManager):
    assert manager.get_meta("schema_version") == "3.2"
    assert manager.embedding_signature() == "test::dim=8"
    tables = {
        row["name"]
        for row in manager.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    for name in (
        "documents",
        "knowledge_cards",
        "knowledge_fts",
        "raw_evidence_chunks",
        "controlled_keywords",
        "notation_map",
        "tasks",
        "events",
    ):
        assert name in tables
    assert manager.conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_document_partial_merge_and_doi_dedup(manager: SQLiteManager):
    doc_id = _document(manager)
    same = manager.upsert_document(
        {
            "doi": "doi:10.1234/ACDB.V2.001",
            "local_pdf_path": "storage/x.pdf",
            "download_status": "DOWNLOADED",
            "metadata_json": {"ingest": "test"},
        }
    )
    assert same == doc_id
    doc = manager.get_document(doc_id)
    assert doc["authors_list"] == ["A. Author"]
    assert doc["journal"] == "Physical Review A"
    assert doc["abstract"].startswith("MCDHF")
    assert doc["download_status"] == "DOWNLOADED"
    assert doc["metadata"]["ingest"] == "test"


def test_card_fts_triggers_filters_and_delete(manager: SQLiteManager):
    doc_id = _document(manager)
    knw_id = _card(manager, doc_id)
    hits = manager.search_cards_fts("MCDHF")
    assert [hit["knw_id"] for hit in hits] == [knw_id]
    assert hits[0]["bm25_score"] <= 0
    chinese = manager.search_cards_fts("芯价关联")
    assert [hit["knw_id"] for hit in chinese] == [knw_id]

    # Metadata pre-filter
    assert manager.list_knowledge_cards(
        filters={"isoelectronic": "Ne-like", "z_num": 26, "method": "MCDHF"}
    )[0]["knw_id"] == knw_id
    assert manager.list_knowledge_cards(filters={"element": "Xe"}) == []

    # Update must replace the FTS row, not duplicate it.
    manager.upsert_knowledge_card(
        {"knw_id": knw_id, "summary_zh": "QED corrections and nuclear recoil"}
    )
    assert manager.get_knowledge_card(knw_id)["premises_and_conditions"] == "适用于 Ne-like 高电荷态离子"
    assert manager.search_cards_fts("芯价关联") == []
    assert manager.search_cards_fts("QED")[0]["knw_id"] == knw_id

    assert manager.delete_knowledge_card(knw_id)
    assert manager.search_cards_fts("QED") == []


def test_l1_master_card_and_conflict(manager: SQLiteManager):
    doc_id = _document(manager)
    master_id = manager.upsert_knowledge_card(
        {
            "card_level": "L1",
            "card_type": "MASTER",
            "title_zh": "类氖离子总纲",
            "summary_zh": "类氖等电子序列的 MCDHF 计算总纲",
            "isoelectronic": "Ne-like",
            "method": "MCDHF",
            "supporting_doc_ids": [doc_id],
        }
    )
    assert master_id == "MASTER-NELIKE-MCDHF"
    assert manager.stats()["l1_cards"] == 1

    card = _card(manager, doc_id, has_conflict=1, conflict_with_knw_ids=[master_id])
    assert manager.conflict_cards()[0]["knw_id"] == card
    assert manager.get_knowledge_card(card)["conflict_with_knw_ids_list"] == [master_id]


def test_evidence_keywords_tasks_and_notation(manager: SQLiteManager):
    doc_id = _document(manager)
    knw_id = _card(manager, doc_id)
    chunk = manager.add_evidence(
        knw_id,
        doc_id,
        "⟪FOCUS⟫ the Breit interaction reduces the discrepancy",
        section_title="II.B",
        page_num=4,
    )
    assert manager.list_evidence(knw_id)[0]["chunk_id"] == chunk
    kw_id = manager.upsert_keyword(
        "core-valence correlation",
        term_zh="芯价关联",
        synonyms=["CV correlation"],
        category="CORRELATION",
    )
    manager.link_card_keywords(knw_id, [kw_id])
    assert manager.keywords_for_card(knw_id)[0]["kw_id"] == kw_id

    manager.upsert_notation("bar{2p}", "2p_{1/2}", "ORBITAL")
    assert manager.normalize_symbol("bar{2p}") == "2p_{1/2}"

    task = manager.add_task("PARSE", doc_id=doc_id, payload={"pages": 3}, priority=5)
    claimed = manager.claim_next_task()
    assert claimed["task_id"] == task
    manager.finish_task(task, success=True)
    assert manager.count_tasks()["DONE"] == 1

    with pytest.raises(sqlite3.IntegrityError):
        manager.conn.execute(
            "INSERT INTO raw_evidence_chunks(chunk_id,parent_knw_id,doc_id,context_bundle_md) "
            "VALUES ('EV-BAD','KNW-MISSING',?, 'x')",
            (doc_id,),
        )
    manager.conn.rollback()


def test_embedding_signature_lock(tmp_path: Path):
    workspace = tmp_path / "workspace"
    SQLiteManager(workspace, embedding_signature="A::dim=1024").close()
    db = SQLiteManager(workspace, embedding_signature="B::dim=1024")
    assert db.embedding_signature_mismatch is True
    with pytest.raises(EmbeddingSignatureMismatch):
        db.assert_embedding_signature("B::dim=1024")
    db.close()


def test_v1_database_is_backed_up(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    path = workspace / "atomic_meta.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);"
        "CREATE VIRTUAL TABLE knowledge_fts USING fts5(knw_id, title);"
        "INSERT INTO meta VALUES('schema_version','1');"
    )
    conn.commit()
    conn.close()

    db = SQLiteManager(workspace)
    backups = list(workspace.glob("atomic_meta_v1_backup_*.sqlite"))
    assert backups
    assert db.get_meta("schema_version") == "3.2"
    db.close()


# ---------------------------------------------------------------- Qdrant V2
def test_qdrant_roundtrip_and_filter(tmp_path: Path):
    qdrant = pytest.importorskip("qdrant_client")
    manager = QdrantManager(
        tmp_path / "workspace",
        dimension=8,
        embedding_signature="test::dim=8",
    )
    if not manager.available:
        pytest.skip(f"Qdrant local unavailable: {manager.load_error}")
    manager.upsert_card(
        "KNW-STRAT-2024-ABCDEF",
        [1, 0, 0, 0, 0, 0, 0, 0],
        {"card_type": "STRAT", "method": "MCDHF", "z_num": 26},
    )
    manager.upsert_card(
        "KNW-MODEL-2024-ABCDEF",
        [0, 1, 0, 0, 0, 0, 0, 0],
        {"card_type": "MODEL", "method": "RMBPT", "z_num": 26},
    )
    assert manager.count() == 2
    hits = manager.search([1, 0, 0, 0, 0, 0, 0, 0], limit=5)
    assert hits[0]["knw_id"] == "KNW-STRAT-2024-ABCDEF"
    filtered = manager.search(
        [1, 0, 0, 0, 0, 0, 0, 0], filters={"card_type": "MODEL"}, limit=5
    )
    assert [hit["knw_id"] for hit in filtered] == ["KNW-MODEL-2024-ABCDEF"]
    manager.close()

    reopened = QdrantManager(
        tmp_path / "workspace", dimension=8, embedding_signature="other::dim=8"
    )
    assert reopened.signature_mismatch is True
    assert reopened.status().ok is False
    reopened.close()


def test_clear_demo_removes_derived_cards_and_evidence(tmp_path: Path):
    from db.demo_v2 import seed_demo_v2

    db = SQLiteManager(tmp_path / "workspace", embedding_signature="test::dim=8")
    seed_demo_v2(db)
    real_doc = db.upsert_document(
        {
            "doc_type": "PAP",
            "title": "A real user paper",
            "pub_year": 2025,
            "download_status": "DOWNLOADED",
            "metadata_json": {"demo": False},
        }
    )
    mixed_card = db.upsert_knowledge_card(
        {
            "card_level": "L2",
            "card_type": "CONC",
            "title_zh": "同时引用示例与真实文献的卡片",
            "summary_zh": "clearing demo data must keep this curated card",
            "supporting_doc_ids": [real_doc, "DOC-PAP-2022-C0FFEE"],
        }
    )
    before = db.stats()
    assert before["documents"] == 5
    cleared = db.clear_demo_documents()
    assert cleared == 4
    after = db.stats()
    assert after["documents"] == 1
    assert after["evidence"] == 0
    assert after["keywords"] == 0
    assert after["pending_tasks"] == 0
    assert db.get_meta("demo_keyword_ids") is None
    remaining = db.get_knowledge_card(mixed_card)
    assert remaining is not None
    assert remaining["supporting_doc_ids_list"] == [real_doc]
    assert [card["knw_id"] for card in db.list_knowledge_cards(limit=100)] == [mixed_card]
    assert not [event for event in db.recent_events(limit=100) if event["category"] == "demo"]
    db.close()
