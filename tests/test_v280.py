import json
from pathlib import Path

import pytest
from PySide6.QtCore import Qt

from config import AppConfig
from db.demo_v2 import seed_demo_v2
from db.sqlite_manager import SQLiteManager
from models.pipeline_stepper import compute_document_pipeline_status
from ui.views.library import LibraryView


def test_v280_summary_projection_and_cached_pipeline(tmp_path: Path):
    db = SQLiteManager(tmp_path)
    seed_demo_v2(db)

    # Verify summary returns lean dicts without heavy columns
    summary = db.list_documents_summary(limit=10, offset=0)
    assert len(summary) >= 4
    first = summary[0]
    # Required summary keys
    assert "doc_id" in first
    assert "title" in first
    assert "card_count" in first
    assert "has_master" in first
    assert "pipeline_step" in first
    assert "pipeline_glow" in first
    # Heavy keys should NOT be in summary
    assert "abstract" not in first
    assert "metadata_json" not in first
    assert "error_log" not in first

    # Verify backfill cached values
    doc_id = first["doc_id"]
    cards = db.cards_for_document(doc_id)
    expected_status = compute_document_pipeline_status(first, cards)
    assert first["card_count"] == len(cards)
    assert first["pipeline_step"] == expected_status.current_step
    assert first["pipeline_glow"] == expected_status.glow.value


def test_v280_cache_update_on_card_upsert_and_delete(tmp_path: Path):
    db = SQLiteManager(tmp_path)
    # Insert a document
    doc_id = "DOC-PAP-2026-F1A2B3"
    db.upsert_document({
        "doc_id": doc_id,
        "title": "Test Plasma Paper for Cache",
        "doc_type": "PAP",
        "pub_year": 2026,
        "download_status": "DOWNLOADED",
        "parse_status": "LLM_EXTRACTED",
    })

    doc_before = db.list_documents_summary(limit=1, offset=0)[0]
    assert doc_before["card_count"] == 0
    assert doc_before["has_master"] == 0

    # Upsert an L2 card
    c1 = db.upsert_knowledge_card({
        "knw_id": "KNW-STRAT-2026-A1B2C3",
        "card_level": "L2",
        "card_type": "STRAT",
        "title_zh": "测试策略卡",
        "supporting_doc_ids": [doc_id],
    })
    doc_after1 = db.list_documents_summary(limit=1, offset=0)[0]
    assert doc_after1["card_count"] == 1
    assert doc_after1["has_master"] == 0

    # Upsert an L1 Master card
    c2 = db.upsert_knowledge_card({
        "knw_id": "MASTER-PLASMA-MCDHF",
        "card_level": "L1",
        "card_type": "MASTER",
        "title_zh": "测试主卡",
        "supporting_doc_ids": [doc_id],
    })
    doc_after2 = db.list_documents_summary(limit=1, offset=0)[0]
    assert doc_after2["card_count"] == 2
    assert doc_after2["has_master"] == 1
    assert doc_after2["pipeline_glow"] == "GOLD"

    # Delete master card
    db.delete_knowledge_card(c2)
    doc_after3 = db.list_documents_summary(limit=1, offset=0)[0]
    assert doc_after3["card_count"] == 1
    assert doc_after3["has_master"] == 0
    assert doc_after3["pipeline_glow"] == "GREEN"

    # Delete remaining L2 card
    db.delete_knowledge_card(c1)
    doc_after4 = db.list_documents_summary(limit=1, offset=0)[0]
    assert doc_after4["card_count"] == 0
    assert doc_after4["has_master"] == 0


def test_v280_library_view_pagination(qapp, tmp_path: Path):
    db = SQLiteManager(tmp_path)
    # Seed 60 documents
    for i in range(60):
        hex_id = f"{i:06X}"
        db.upsert_document({
            "doc_id": f"DOC-PAP-2026-{hex_id}",
            "title": f"Document Title {i}",
            "doc_type": "PAP",
            "pub_year": 2020 + (i % 5),
            "download_status": "DOWNLOADED",
            "parse_status": "LIGHT_PARSED",
        })

    view = LibraryView(db, None, str(tmp_path))
    view.refresh()

    # Default page size is 50
    assert view.page_size == 50
    assert view.total_count == 60
    assert view.total_pages == 2
    assert view.current_page == 1
    assert view.table.rowCount() == 50
    assert view.next_page_button.isEnabled()
    assert not view.prev_page_button.isEnabled()

    # Go to next page
    view._go_next_page()
    assert view.current_page == 2
    assert view.table.rowCount() == 10
    assert not view.next_page_button.isEnabled()
    assert view.prev_page_button.isEnabled()

    # Change page size to 25
    view.page_size_combo.setCurrentIndex(0)  # 25 items
    assert view.page_size == 25
    assert view.total_pages == 3
    assert view.current_page == 1
    assert view.table.rowCount() == 25
