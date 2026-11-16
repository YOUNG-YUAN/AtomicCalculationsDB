"""Tests for AtomicCalculationDB V2.9.0.

Covers:
1. SQLite collections CRUD, hierarchies, and document bindings.
2. Filtering and ordering by collection, pub_year, title, authors, and pipeline_step.
3. ReprocessManager.local_clean fine-grained operations and safe cooperative interruption.
4. GUI LibraryView collections tree, header sorting, and LocalCleanDialog.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView, QHeaderView

from app_core.db.sqlite_manager import SQLiteManager
from app_core.reprocess import ReprocessManager
from app_core.ui.local_clean_dialog import DocSelectDialog, LocalCleanDialog
from app_core.ui.views.library import LibraryView, format_authors_short


def test_v290_collections_crud_and_counts(tmp_path: Path):
    db = SQLiteManager(tmp_path)

    # 1. Create collections (hierarchical)
    c_root = db.create_collection("Atomic Calculation")
    assert c_root.startswith("col_")
    c_child = db.create_collection("MCDHF", parent_id=c_root)
    assert c_child.startswith("col_")

    col_list = db.list_collections()
    assert len(col_list) == 2
    root_dict = next(c for c in col_list if c["collection_id"] == c_root)
    child_dict = next(c for c in col_list if c["collection_id"] == c_child)
    assert root_dict["name"] == "Atomic Calculation"
    assert root_dict["parent_id"] is None
    assert child_dict["name"] == "MCDHF"
    assert child_dict["parent_id"] == c_root

    # 2. Rename collection
    assert db.rename_collection(c_child, "MCDHF Theory")
    assert next(c for c in db.list_collections() if c["collection_id"] == c_child)["name"] == "MCDHF Theory"

    # 3. Add documents to collection
    db.upsert_document({
        "doc_id": "DOC-PAP-2026-000001",
        "title": "Lamb Shift Calculation",
        "authors": '["Baumann, P.", "Thijssen, J."]',
        "pub_year": 2005,
    })
    db.upsert_document({
        "doc_id": "DOC-PAP-2026-000002",
        "title": "Relativistic Configuration Interaction",
        "authors": '["Scherer, A."]',
        "pub_year": 2017,
    })

    assert db.count_collection_documents("__all__") == 2
    assert db.count_collection_documents("__unfiled__") == 2

    # File DOC 1 into c_child
    added = db.add_documents_to_collection(c_child, ["DOC-PAP-2026-000001"])
    assert added == 1
    assert db.get_document_collection_ids("DOC-PAP-2026-000001") == [c_child]
    assert db.count_collection_documents(c_child) == 1
    assert db.count_collection_documents("__unfiled__") == 1

    # Idempotent adding
    assert db.add_documents_to_collection(c_child, ["DOC-PAP-2026-000001"]) == 0

    # 4. Remove documents from collection
    removed = db.remove_documents_from_collection(c_child, ["DOC-PAP-2026-000001"])
    assert removed == 1
    assert db.count_collection_documents(c_child) == 0
    assert db.count_collection_documents("__unfiled__") == 2

    # 5. Delete parent collection cascades to child
    db.add_documents_to_collection(c_child, ["DOC-PAP-2026-000001"])
    assert db.delete_collection(c_root)
    assert len(db.list_collections()) == 0
    # Documents table remains untouched
    assert db.count_documents() == 2


def test_v290_summary_filtering_and_sorting(tmp_path: Path):
    db = SQLiteManager(tmp_path)
    col1 = db.create_collection("Physics")

    # Step 1 doc (QUEUED)
    db.upsert_document({
        "doc_id": "DOC-PAP-2026-0000A1",
        "title": "Alpha Spectroscopy",
        "pub_year": 2010,
        "download_status": "QUEUED",
        "parse_status": "PENDING",
    })
    # Step 2 doc (DOWNLOADED, LIGHT_PARSED)
    db.upsert_document({
        "doc_id": "DOC-PAP-2026-0000A2",
        "title": "Beta Decay Dynamics",
        "pub_year": 2022,
        "download_status": "DOWNLOADED",
        "parse_status": "LIGHT_PARSED",
    })
    # Step 4 doc (LLM_EXTRACTED, with cards -> glow green)
    db.upsert_document({
        "doc_id": "DOC-PAP-2026-0000A3",
        "title": "Gamma Ray Burst Summary",
        "pub_year": 2018,
        "download_status": "DOWNLOADED",
        "parse_status": "LLM_EXTRACTED",
    })
    db.upsert_knowledge_card({
        "knw_id": "KNW-STRAT-2026-000001",
        "doc_id": "DOC-PAP-2026-0000A3",
        "card_type": "STRAT",
        "card_level": "L2",
        "title_zh": "Gamma 理论",
        "summary_zh": "测试",
    })

    db.add_documents_to_collection(col1, ["DOC-PAP-2026-0000A1", "DOC-PAP-2026-0000A2"])

    # Test collection filtering
    docs_in_col = db.list_documents_summary(collection_id=col1)
    assert len(docs_in_col) == 2
    assert {d["doc_id"] for d in docs_in_col} == {"DOC-PAP-2026-0000A1", "DOC-PAP-2026-0000A2"}

    # Test unfiled filtering
    docs_unfiled = db.list_documents_summary(collection_id="__unfiled__")
    assert len(docs_unfiled) == 1
    assert docs_unfiled[0]["doc_id"] == "DOC-PAP-2026-0000A3"

    # Test sorting by pub_year ASC / DESC
    sorted_year_asc = db.list_documents_summary(order_by="pub_year ASC")
    assert [d["pub_year"] for d in sorted_year_asc] == [2010, 2018, 2022]

    sorted_year_desc = db.list_documents_summary(order_by="pub_year DESC")
    assert [d["pub_year"] for d in sorted_year_desc] == [2022, 2018, 2010]

    # Test sorting by pipeline_step DESC, pipeline_glow DESC
    sorted_step_desc = db.list_documents_summary(order_by="pipeline_step DESC, pipeline_glow DESC")
    assert [d["pipeline_step"] for d in sorted_step_desc] == [4, 3, 1]


def test_v290_local_clean_granular_and_safe_abort(tmp_path: Path):
    db = SQLiteManager(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    reprocess = ReprocessManager(db, workspace, None)

    # 1. Immediate safe stop
    result_stopped = reprocess.local_clean(
        operations=["metadata", "purge"],
        should_stop=lambda: True,
    )
    assert result_stopped.get("stopped") is True

    # 2. Granular operation execution (only metadata)
    db.upsert_document({
        "doc_id": "DOC-PAP-2026-000099",
        "title": "Short",
        "pub_year": None,
    })
    result_clean = reprocess.local_clean(
        operations=["metadata"],
        doc_ids=["DOC-PAP-2026-000099"],
    )
    assert result_clean.get("stopped") is False
    assert "metadata" in result_clean
    assert result_clean["purged"]["deleted"] == 0


def test_v290_gui_library_view_integration(qapp, tmp_path: Path):
    db = SQLiteManager(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    col_id = db.create_collection("Quantum Physics")
    db.upsert_document({
        "doc_id": "DOC-PAP-2026-000001",
        "title": "Quantum Electro-Dynamics",
        "authors": '["Feynman, R. P.", "Schwinger, J."]',
        "pub_year": 1965,
        "journal": "Phys. Rev.",
    })
    db.add_documents_to_collection(col_id, ["DOC-PAP-2026-000001"])

    view = LibraryView(db, None, str(workspace))
    view.refresh()

    # Verify 10 columns in table
    assert view.table.columnCount() == 10
    assert "作者" in view.table.horizontalHeaderItem(5).text()
    assert view.table.rowCount() == 1
    # Check formatted authors
    assert view.table.item(0, 5).text() == "Feynman, R. P., Schwinger, J."

    # Verify Collections Tree
    assert hasattr(view, "collections_tree")
    assert view.collections_tree.topLevelItemCount() >= 4  # All, Recent, Unfiled, Header, Custom

    # Test Header Click Sorting
    header = view.table.horizontalHeader()
    # Click year column (col 3)
    view._on_header_section_clicked(3)
    assert view.sort_column_index == 3
    assert view.sort_order == "DESC"
    assert "▼" in view.table.horizontalHeaderItem(3).text()

    # Click year column again to toggle ASC
    view._on_header_section_clicked(3)
    assert view.sort_column_index == 3
    assert view.sort_order == "ASC"
    assert "▲" in view.table.horizontalHeaderItem(3).text()

    # Test LocalCleanDialog instantiation
    dlg = LocalCleanDialog(
        view.reprocess_manager,
        selected_doc_ids=["DOC-PAP-2026-000001"],
        total_doc_count=1,
    )
    assert dlg.chk_metadata.isChecked()
    assert dlg.radio_selected.isChecked()
    dlg._deselect_all_ops()
    assert not dlg.chk_metadata.isChecked()
    dlg._select_all_ops()
    assert dlg.chk_metadata.isChecked()
    dlg.close()

    # V2.9.1 (FixTask_10_01) Tests:
    # 1. Interactive column resize & horizontal scrollbar checks
    assert header.sectionsMovable() is False
    for col_idx in range(10):
        assert header.sectionResizeMode(col_idx) == QHeaderView.Interactive
    assert view.table.horizontalScrollBarPolicy() == Qt.ScrollBarAsNeeded
    assert view.table.verticalScrollBarPolicy() == Qt.ScrollBarAsNeeded
    assert view.table.horizontalScrollMode() == QAbstractItemView.ScrollPerPixel
    assert view.splitter.childrenCollapsible() is False

    # 2. Test safe collection creation (including parent_id=False from clicked signal)
    with patch("PySide6.QtWidgets.QInputDialog.getText", return_value=("Laser Physics", True)):
        view._create_new_collection_prompt(parent_id=False)  # Simulates QPushButton.clicked(False)
        assert view.current_collection_id.startswith("col_")
        assert any(c["name"] == "Laser Physics" for c in db.list_collections())

    # 3. Test safe child collection creation
    with patch("PySide6.QtWidgets.QInputDialog.getText", return_value=("Optics", True)):
        view._create_new_collection_prompt(parent_id=col_id, parent_name="Quantum Physics")
        optics_col = next(c for c in db.list_collections() if c["name"] == "Optics")
        assert optics_col["parent_id"] == col_id

    # V2.9.2 (FixTask_10_02) Tests:
    # 1. LocalCleanDialog scope selection quick buttons
    clean_dlg = LocalCleanDialog(
        view.reprocess_manager,
        selected_doc_ids=["DOC-PAP-2026-000001"],
        total_doc_count=db.count_documents(),
        db=db,
    )
    assert clean_dlg.btn_select_all_docs.isEnabled()
    assert clean_dlg.btn_clear_all_docs.isEnabled()
    assert clean_dlg.btn_open_doc_select.isEnabled()
    # Test clear selection
    clean_dlg._clear_all_docs()
    assert len(clean_dlg.selected_doc_ids) == 0
    assert not clean_dlg.radio_selected.isEnabled()
    # Test select all docs
    clean_dlg._select_all_docs()
    assert clean_dlg.radio_all.isChecked()
    assert len(clean_dlg.selected_doc_ids) >= 1
    clean_dlg.close()

    # 2. DocSelectDialog (point, box, range selection table + hierarchical directory tree)
    doc_dlg = DocSelectDialog(db=db, initial_selected_ids=["DOC-PAP-2026-000001"])
    assert doc_dlg.table.selectionMode() == QAbstractItemView.ExtendedSelection
    assert doc_dlg.table.selectionBehavior() == QAbstractItemView.SelectRows
    assert doc_dlg.table.horizontalScrollBarPolicy() == Qt.ScrollBarAsNeeded
    assert doc_dlg.table.horizontalScrollMode() == QAbstractItemView.ScrollPerPixel
    assert "DOC-PAP-2026-000001" in doc_dlg.get_selected_doc_ids()

    # V2.9.3 (FixTask_10_03) Directory Tree in DocSelectDialog:
    assert hasattr(doc_dlg, "collections_tree")
    assert doc_dlg.collections_tree.topLevelItemCount() >= 4  # All, Recent, Unfiled, Header/Custom
    assert hasattr(doc_dlg, "btn_new_collection")
    assert hasattr(doc_dlg, "splitter")
    assert doc_dlg.splitter.childrenCollapsible() is False

    # Test clicking collection item
    all_item = doc_dlg.collections_tree.topLevelItem(0)
    doc_dlg._on_collection_item_clicked(all_item, 0)
    assert doc_dlg.current_collection_id == "__all__"
    assert doc_dlg.table.rowCount() >= 1

    # Test creating new category in DocSelectDialog
    with patch("PySide6.QtWidgets.QInputDialog.getText", return_value=("DocSelect New Folder", True)):
        doc_dlg._create_new_collection_prompt()
        assert any(c["name"] == "DocSelect New Folder" for c in db.list_collections())

    # Test select all
    doc_dlg._select_all_rows()
    assert len(doc_dlg.get_selected_doc_ids()) == doc_dlg.table.rowCount()

    # Test deselect all
    doc_dlg._deselect_all_rows()
    assert len(doc_dlg.get_selected_doc_ids()) == 0

    # Test invert selection
    doc_dlg._invert_selection()
    assert len(doc_dlg.get_selected_doc_ids()) == doc_dlg.table.rowCount()

    # Test search filter
    doc_dlg.search_edit.setText("Electro-Dynamics")
    assert doc_dlg.table.rowCount() >= 1
    doc_dlg.search_edit.setText("NO_SUCH_DOCUMENT_999999")
    assert doc_dlg.table.rowCount() == 0
    doc_dlg.search_edit.setText("")
    assert doc_dlg.table.rowCount() >= 1
    doc_dlg.close()


def test_task_scope_dialog_and_deep_synthesis_multidoc(tmp_path: Path, qapp) -> None:
    db = SQLiteManager(tmp_path)
    doc_1 = {
        "doc_id": "DOC-PAP-2024-000001",
        "title": "Quantum Electrodynamics in Heavy Atoms",
        "doc_type": "PAP",
        "pub_year": 2024,
        "journal": "Phys. Rev. A",
        "authors": json.dumps(["Johnson, W. R."]),
        "download_status": "DOWNLOADED",
        "parse_status": "LLM_EXTRACTED",
    }
    doc_2 = {
        "doc_id": "DOC-PAP-2025-000002",
        "title": "Relativistic Many-Body Perturbation Theory",
        "doc_type": "PAP",
        "pub_year": 2025,
        "journal": "Phys. Rev. Lett.",
        "authors": json.dumps(["Grant, I. P."]),
        "download_status": "DOWNLOADED",
        "parse_status": "LLM_EXTRACTED",
    }
    db.upsert_document(doc_1)
    db.upsert_document(doc_2)

    from app_core.ui.task_scope_dialog import TaskScopeDialog
    from app_core.ui.deep_synthesis_dialog import DeepSynthesisDialog

    # 1. Test TaskScopeDialog for step3_api
    dlg_api = TaskScopeDialog(
        db=db,
        task_type="step3_api",
        selected_doc_ids=["DOC-PAP-2024-000001"],
        filtered_doc_ids=["DOC-PAP-2024-000001", "DOC-PAP-2025-000002"],
        total_doc_count=2,
    )
    assert dlg_api.radio_selected.isChecked()
    assert dlg_api.get_target_doc_ids() == ["DOC-PAP-2024-000001"]

    # Toggle radio_filtered
    dlg_api.radio_filtered.setChecked(True)
    assert dlg_api.get_target_doc_ids() == ["DOC-PAP-2024-000001", "DOC-PAP-2025-000002"]

    # Toggle radio_all
    dlg_api.radio_all.setChecked(True)
    assert dlg_api.get_target_doc_ids() is None

    # Test quick buttons
    dlg_api._clear_all_docs()
    assert len(dlg_api.selected_doc_ids) == 0
    assert not dlg_api.radio_selected.isEnabled()

    dlg_api._select_all_docs()
    assert len(dlg_api.selected_doc_ids) == 2
    assert dlg_api.radio_selected.isEnabled()
    dlg_api.close()

    # 2. Test TaskScopeDialog for step3_semi and step5_deep
    dlg_semi = TaskScopeDialog(db=db, task_type="step3_semi")
    assert "网页版" in dlg_semi.windowTitle()
    dlg_semi.close()

    dlg_deep = TaskScopeDialog(db=db, task_type="step5_deep")
    assert "深度归纳" in dlg_deep.windowTitle()
    dlg_deep.close()

    # 3. Test DeepSynthesisDialog with multi-doc support
    synth_dlg = DeepSynthesisDialog(
        db=db,
        workspace=tmp_path,
        doc_id=["DOC-PAP-2024-000001", "DOC-PAP-2025-000002"],
    )
    assert synth_dlg.doc_ids == ["DOC-PAP-2024-000001", "DOC-PAP-2025-000002"]
    assert synth_dlg.doc_id == "DOC-PAP-2024-000001"
    assert hasattr(synth_dlg, "doc_combo") and synth_dlg.doc_combo is not None
    assert synth_dlg.doc_combo.count() == 2

    # Switch doc via combo
    synth_dlg.doc_combo.setCurrentIndex(1)
    assert synth_dlg.doc_id == "DOC-PAP-2025-000002"
    assert "DOC-PAP-2025-000002" in synth_dlg.windowTitle()
    synth_dlg.close()
