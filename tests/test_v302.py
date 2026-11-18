"""Unit tests for v3.0.2:
- DocSelectDialog table header clicking for multi-dimensional sorting (ascending / descending)
- Preserving row selections across sorts and filters
- Verifying version bump to v3.0.2
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock
import pytest
from PySide6.QtCore import Qt

from db.sqlite_manager import SQLiteManager
from app_core.ui.local_clean_dialog import DocSelectDialog, _DOC_SELECT_BASE_HEADER_LABELS


def test_v302_version_json():
    version_file = Path(__file__).resolve().parents[1] / "app_core" / "version.json"
    assert version_file.is_file()
    data = json.loads(version_file.read_text(encoding="utf-8"))
    assert data.get("version") >= "3.0.2"


def test_doc_select_dialog_header_sort_click(tmp_path, qapp):
    db = SQLiteManager(tmp_path)
    # Insert docs with different pub_years and titles
    db.upsert_document({"doc_id": "DOC-PAP-1935-000001", "title": "Alpha Relativistic", "pub_year": 1935, "journal": "Proc R Soc"})
    db.upsert_document({"doc_id": "DOC-PAP-2015-000002", "title": "Beta Er3+ Level", "pub_year": 2015, "journal": "JQSRT"})
    db.upsert_document({"doc_id": "DOC-PAP-2026-000003", "title": "Gamma Calculations", "pub_year": 2026, "journal": "ApJS"})

    dlg = DocSelectDialog(db=db, initial_selected_ids=["DOC-PAP-2015-000002"])
    assert dlg.table.rowCount() == 3

    # Initially no sort indicator on header items
    for idx, label in enumerate(_DOC_SELECT_BASE_HEADER_LABELS):
        assert dlg.table.horizontalHeaderItem(idx).text() == label

    # 1. Click column 3 (年份): default to DESC (newest first: 2026 -> 2015 -> 1935)
    dlg._on_header_section_clicked(3)
    assert dlg.sort_column_index == 3
    assert dlg.sort_order == "DESC"
    assert "年份 ▼" in dlg.table.horizontalHeaderItem(3).text()
    assert dlg.table.item(0, 0).text() == "DOC-PAP-2026-000003"
    assert dlg.table.item(1, 0).text() == "DOC-PAP-2015-000002"
    assert dlg.table.item(2, 0).text() == "DOC-PAP-1935-000001"

    # 2. Click column 3 (年份) again: toggle to ASC (1935 -> 2015 -> 2026)
    dlg._on_header_section_clicked(3)
    assert dlg.sort_column_index == 3
    assert dlg.sort_order == "ASC"
    assert "年份 ▲" in dlg.table.horizontalHeaderItem(3).text()
    assert dlg.table.item(0, 0).text() == "DOC-PAP-1935-000001"
    assert dlg.table.item(1, 0).text() == "DOC-PAP-2015-000002"
    assert dlg.table.item(2, 0).text() == "DOC-PAP-2026-000003"

    # 3. Click column 1 (标题): default to ASC (Alpha -> Beta -> Gamma)
    dlg._on_header_section_clicked(1)
    assert dlg.sort_column_index == 1
    assert dlg.sort_order == "ASC"
    assert "标题 ▲" in dlg.table.horizontalHeaderItem(1).text()
    assert "年份" == dlg.table.horizontalHeaderItem(3).text()  # Previous arrow cleared
    assert dlg.table.item(0, 1).text() == "Alpha Relativistic"
    assert dlg.table.item(1, 1).text() == "Beta Er3+ Level"
    assert dlg.table.item(2, 1).text() == "Gamma Calculations"

    # 4. Click column 1 (标题) again: toggle to DESC (Gamma -> Beta -> Alpha)
    dlg._on_header_section_clicked(1)
    assert dlg.sort_column_index == 1
    assert dlg.sort_order == "DESC"
    assert "标题 ▼" in dlg.table.horizontalHeaderItem(1).text()
    assert dlg.table.item(0, 1).text() == "Gamma Calculations"
    assert dlg.table.item(1, 1).text() == "Beta Er3+ Level"
    assert dlg.table.item(2, 1).text() == "Alpha Relativistic"

    # 5. Click column 0 (DOC-ID): default to ASC
    dlg._on_header_section_clicked(0)
    assert dlg.sort_column_index == 0
    assert dlg.sort_order == "ASC"
    assert "DOC-ID ▲" in dlg.table.horizontalHeaderItem(0).text()
    assert dlg.table.item(0, 0).text() == "DOC-PAP-1935-000001"
    assert dlg.table.item(1, 0).text() == "DOC-PAP-2015-000002"
    assert dlg.table.item(2, 0).text() == "DOC-PAP-2026-000003"

    dlg.close()


def test_doc_select_dialog_sorting_preserves_selection(tmp_path, qapp):
    db = SQLiteManager(tmp_path)
    db.upsert_document({"doc_id": "DOC-PAP-1935-000001", "title": "Doc A", "pub_year": 1935})
    db.upsert_document({"doc_id": "DOC-PAP-2015-000002", "title": "Doc B", "pub_year": 2015})
    db.upsert_document({"doc_id": "DOC-PAP-2026-000003", "title": "Doc C", "pub_year": 2026})

    # Start with Doc B selected
    dlg = DocSelectDialog(db=db, initial_selected_ids=["DOC-PAP-2015-000002"])
    assert dlg.get_selected_doc_ids() == ["DOC-PAP-2015-000002"]

    # Sort DESC by year
    dlg._on_header_section_clicked(3)
    assert dlg.get_selected_doc_ids() == ["DOC-PAP-2015-000002"]
    # Check that row 1 (which is Doc B in 2026, 2015, 1935) is visually selected
    assert dlg.table.item(1, 0).isSelected()

    # Sort ASC by year
    dlg._on_header_section_clicked(3)
    assert dlg.get_selected_doc_ids() == ["DOC-PAP-2015-000002"]
    assert dlg.table.item(1, 0).isSelected()

    # Also select Doc A (now at row 0)
    dlg.table.item(0, 0).setSelected(True)
    dlg._on_selection_changed()
    assert set(dlg.get_selected_doc_ids()) == {"DOC-PAP-1935-000001", "DOC-PAP-2015-000002"}

    # Sort by title ASC
    dlg._on_header_section_clicked(1)
    assert set(dlg.get_selected_doc_ids()) == {"DOC-PAP-1935-000001", "DOC-PAP-2015-000002"}

    dlg.close()


def test_doc_select_dialog_search_retains_sort(tmp_path, qapp):
    db = SQLiteManager(tmp_path)
    db.upsert_document({"doc_id": "DOC-PAP-1935-000001", "title": "Slater Field", "pub_year": 1935})
    db.upsert_document({"doc_id": "DOC-PAP-2015-000002", "title": "Field Calculations", "pub_year": 2015})
    db.upsert_document({"doc_id": "DOC-PAP-2026-000003", "title": "Another Paper", "pub_year": 2026})

    dlg = DocSelectDialog(db=db)
    # Sort DESC by year
    dlg._on_header_section_clicked(3)
    assert dlg.table.item(0, 0).text() == "DOC-PAP-2026-000003"

    # Search for "Field" (matches 2015 and 1935)
    dlg.search_edit.setText("Field")
    assert dlg.table.rowCount() == 2
    # Should retain DESC year order: 2015 then 1935
    assert dlg.table.item(0, 0).text() == "DOC-PAP-2015-000002"
    assert dlg.table.item(1, 0).text() == "DOC-PAP-1935-000001"

    # Clear search
    dlg.search_edit.setText("")
    assert dlg.table.rowCount() == 3
    assert dlg.table.item(0, 0).text() == "DOC-PAP-2026-000003"
    assert dlg.table.item(1, 0).text() == "DOC-PAP-2015-000002"
    assert dlg.table.item(2, 0).text() == "DOC-PAP-1935-000001"

    dlg.close()
