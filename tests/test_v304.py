"""Unit tests for v3.0.4:
- Smart hierarchical recovery for semi-auto unfinished runs (exact, subset, intersection, most recent)
- Manifest document synchronization in SemiAutoBatchDialog
- Version bump to v3.0.4
"""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from models.semi_auto import find_unfinished_run, save_run


def test_v304_version_json():
    version_file = Path(__file__).resolve().parents[1] / "app_core" / "version.json"
    assert version_file.is_file()
    data = json.loads(version_file.read_text(encoding="utf-8"))
    assert data.get("version") >= "3.0.4"


def test_find_unfinished_run_hierarchical_matching(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()

    # Create run A with 3 documents
    manifest_a = {
        "run_id": "semi_20261118_010000_111111",
        "status": "WAITING",
        "mode": "force",
        "documents": [
            {"doc_id": "DOC-A"},
            {"doc_id": "DOC-B"},
            {"doc_id": "DOC-C"},
        ],
        "batches": [],
    }
    save_run(ws, manifest_a)

    # 1. Exact match (3 documents)
    found = find_unfinished_run(ws, ["DOC-A", "DOC-B", "DOC-C"])
    assert found is not None
    assert found["run_id"] == "semi_20261118_010000_111111"

    # 2. Subset match: Single document DOC-A
    found_single = find_unfinished_run(ws, ["DOC-A"])
    assert found_single is not None
    assert found_single["run_id"] == "semi_20261118_010000_111111"

    # 3. Subset match: Two documents DOC-B + DOC-C
    found_two = find_unfinished_run(ws, ["DOC-B", "DOC-C"])
    assert found_two is not None
    assert found_two["run_id"] == "semi_20261118_010000_111111"

    # 4. Intersection match: DOC-C + DOC-UNKNOWN
    found_inter = find_unfinished_run(ws, ["DOC-C", "DOC-UNKNOWN"])
    assert found_inter is not None
    assert found_inter["run_id"] == "semi_20261118_010000_111111"

    # 5. Completely unrelated document
    found_none = find_unfinished_run(ws, ["DOC-UNRELATED"])
    assert found_none is None

    # 6. Empty doc_ids returns most recent unfinished run
    found_empty = find_unfinished_run(ws, [])
    assert found_empty is not None
    assert found_empty["run_id"] == "semi_20261118_010000_111111"


def test_get_card_model_name_ignores_physics_model():
    from ui.dialogs import get_card_model_name

    # Card has physics theory model 'Born-Oppenheimer' in structured
    card = {
        "knw_id": "KNW-01",
        "structured": {
            "model": "Born-Oppenheimer",
            "table": "Table 1",
        },
        "structured_details_json": json.dumps({"model": "Born-Oppenheimer"}),
    }
    # But provenance has real AI model
    prov = {
        "fast_llm": {
            "model": "deepseek-v4.1-flash-1017",
            "provider": "api",
        }
    }
    # It MUST return the AI model from provenance, not the physics model!
    resolved = get_card_model_name(card, prov)
    assert resolved == "deepseek-v4.1-flash-1017"

    # Even without provenance, it should not return physics model
    resolved_no_prov = get_card_model_name(card, None, default_model="GPT-6.1 Sol Ultra")
    assert resolved_no_prov == "GPT-6.1 Sol Ultra"


def test_library_view_set_reprocess_busy():
    from ui.views.library import LibraryView

    # Verify _set_reprocess_busy does not reference 'step3_semi_button'
    import inspect
    source = inspect.getsource(LibraryView._set_reprocess_busy)
    assert "step3_semi_button" not in source


def test_dialog_window_controls_and_minmax_flags(qapp, tmp_path):
    from PySide6.QtCore import Qt, QEvent
    from PySide6.QtWidgets import QWidget
    from ui.semi_auto_dialog import SemiAutoBatchDialog
    from ui.deep_synthesis_dialog import DeepSynthesisDialog
    from ui.audit_dialogs import DocumentAuditDialog

    parent = QWidget()

    # 1. SemiAutoBatchDialog
    semi = SemiAutoBatchDialog(None, tmp_path, None, [], parent=parent)
    flags = semi.windowFlags()
    assert bool(flags & Qt.Window)
    assert bool(flags & Qt.WindowMinimizeButtonHint)
    assert bool(flags & Qt.WindowMaximizeButtonHint)
    assert bool(flags & Qt.WindowCloseButtonHint)

    # Test changeEvent execution
    evt = QEvent(QEvent.WindowStateChange)
    semi.changeEvent(evt)
    semi.close()

    # 2. DeepSynthesisDialog
    deep = DeepSynthesisDialog(None, tmp_path, "DOC-1", parent=parent)
    flags = deep.windowFlags()
    assert bool(flags & Qt.Window)
    assert bool(flags & Qt.WindowMinimizeButtonHint)
    assert bool(flags & Qt.WindowMaximizeButtonHint)
    assert bool(flags & Qt.WindowCloseButtonHint)
    deep.changeEvent(evt)
    deep.close()

    # 3. DocumentAuditDialog
    audit = DocumentAuditDialog(None, tmp_path, "DOC-1", parent=parent)
    flags = audit.windowFlags()
    assert bool(flags & Qt.Window)
    assert bool(flags & Qt.WindowMinimizeButtonHint)
    assert bool(flags & Qt.WindowMaximizeButtonHint)
    assert bool(flags & Qt.WindowCloseButtonHint)
    audit.changeEvent(evt)
    audit.close()
    parent.close()


