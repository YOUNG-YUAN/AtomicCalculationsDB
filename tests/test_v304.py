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
