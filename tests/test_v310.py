"""Unit tests for v3.1.0:
- Metadata auto-calibration from MinerU Markdown (clean_publisher_banners, extract_metadata_from_markdown, extract_metadata_for_document)
- Elimination of false metadata (e.g. IOP download banner year 2015 and "science" journal)
- DocumentEditDialog with read-only DOC-ID and full field modification
- MetadataDiffDialog for smart diff comparison and selective calibration
- LibraryView table edit triggers set to NoEditTriggers to prevent accidental cell editing on detail close
- Version check for v3.1.0
"""

from __future__ import annotations

import json
from pathlib import Path
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView, QApplication

from unittest.mock import MagicMock
from db.sqlite_manager import SQLiteManager
from parser.metadata_extractor import (
    clean_publisher_banners,
    extract_metadata_for_document,
    extract_metadata_from_markdown,
)
from ui.dialogs import DocumentEditDialog, MetadataDiffDialog
from ui.views.library import LibraryView


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_v310_version_json():
    version_file = Path(__file__).resolve().parents[1] / "app_core" / "version.json"
    assert version_file.is_file()
    data = json.loads(version_file.read_text(encoding="utf-8"))
    assert data.get("version") == "3.1.0"
    assert data.get("build") == "20261119"


def test_clean_publisher_banners():
    sample_text = """Home Search Collections Journals About Contact us My IOPscience
## Some Title
This content has been downloaded from IOPscience. Please scroll down to see the full text.
# 1982 J. Phys. B: At. Mol. Phys. 15 1319
Download details:
IP Address: 142.103.160.110 This content was downloaded on 06/09/2015 at 21:58
Please note that terms and conditions apply.
Printed in Great Britain
Real content starts here
"""
    cleaned = clean_publisher_banners(sample_text)
    assert not any("download details" in line.lower() for line in cleaned)
    assert not any("downloaded on" in line.lower() for line in cleaned)
    assert not any("iopscience" in line.lower() for line in cleaned)
    assert any("1982 J. Phys. B" in line for line in cleaned)
    assert "Real content starts here" in cleaned


def test_extract_metadata_from_markdown_iop_pyper1982():
    # Simulates MinerU parsed.md of DOC-PAP-0000-3D72D4
    md = """Home Search Collections Journals About Contact us My IOPscience

## Analysis of fine-structure excitation energies in Dirac-Fock and perturbation theories

This content has been downloaded from IOPscience. Please scroll down to see the full text.

# 1982 J. Phys. B: At. Mol. Phys. 15 1319

(http://iopscience.iop.org/0022-3700/15/9/009)

View the table of contents for this issue, or go to the journal homepage for more

Download details:

IP Address: 142.103.160.110 This content was downloaded on 06/09/2015 at 21:58

Please note that terms and conditions apply.

J. Phys. B: At. Mol. Phys. 15 (1982) 1319-1331. Printed in Great Britain

# Analysis of fine-structure excitation energies in Dirac-Fock and perturbation theories

N C Pyper?, S J Rose+ and I P Grant§

Department of Theoretical Chemistry, University Chemical Laboratory, Lensfield Road, Cambridge CB2 lEW, England

Received 19 March 1981, in final form 25 January 1982

Abstract, This paper compares total fine structure and its components predicted by Dirac-Fock calculations...
"""
    meta = extract_metadata_from_markdown(md, filename="pyper1982.pdf", allow_network=False)
    assert meta.title == "Analysis of fine-structure excitation energies in Dirac-Fock and perturbation theories"
    # Year must be 1982 (publication year), NEVER 2015 (download year)
    assert meta.year == 1982
    # Journal must be J. Phys. B, NEVER "science"
    assert "J. Phys. B" in meta.journal
    assert "science" != meta.journal.lower()
    # Authors correctly parsed
    assert meta.authors == ["N C Pyper", "S J Rose", "I P Grant"]
    # DOI deduced from IOP URL
    assert meta.doi == "10.1088/0022-3700/15/9/009"


def test_extract_metadata_from_markdown_pra():
    md = """PHYSICAL REVIEW A 81 , 022508 (2010)

## Correlation, relativistic, and quantum electrodynamics effects on the atomic

## structure of eka-thorium

Gediminas Gaigalas, Erikas Gaidamauskas, and Zenonas Rudzikas Vilnius University Research Institute...

Nicola Magnani and Roberto Caciuffo European Commission, Joint Research Centre...

(Received 15 December 2009; published 22 February 2010)

DOI: 10.1103/PhysRevA.81.022508
"""
    meta = extract_metadata_from_markdown(md, allow_network=False)
    assert "Correlation, relativistic, and quantum electrodynamics" in meta.title
    assert "eka-thorium" in meta.title
    assert meta.year == 2010
    assert "Physical Review A" in meta.journal or "PHYSICAL REVIEW A" in meta.journal
    assert meta.doi == "10.1103/PhysRevA.81.022508"


def test_extract_metadata_for_document(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    db = SQLiteManager(tmp_path / "test.db")
    doc_id = db.upsert_document({
        "title": "Old Inaccurate Title",
        "doc_type": "PAP",
        "pub_year": 2015,
        "journal": "science",
    })

    # Put a parsed.md in storage
    doc_dir = ws / "storage" / doc_id
    doc_dir.mkdir(parents=True)
    md_file = doc_dir / "parsed.md"
    md_file.write_text(
        "# 1982 J. Phys. B: At. Mol. Phys. 15 1319\n\n"
        "# Analysis of fine-structure excitation energies\n\n"
        "N C Pyper and I P Grant\n\n"
        "Abstract. Some text here...\n",
        encoding="utf-8",
    )

    meta = extract_metadata_for_document(db, ws, doc_id, allow_network=False)
    assert meta.title == "Analysis of fine-structure excitation energies"
    assert meta.year == 1982
    assert "J. Phys. B" in meta.journal


def test_document_edit_dialog(qapp, tmp_path):
    db = SQLiteManager(tmp_path / "test_edit.db")
    doc_id = db.upsert_document({
        "title": "Initial Title Before Edit",
        "doc_type": "PAP",
        "pub_year": 2000,
        "journal": "Old Journal",
        "authors": ["Author A"],
        "doi": "10.1000/123",
    })
    doc = db.get_document(doc_id)
    assert doc is not None

    dialog = DocumentEditDialog(db, doc)
    # Check that DOC-ID is strictly read-only and disabled
    assert dialog.id_edit.isReadOnly()
    assert not dialog.id_edit.isEnabled()
    assert dialog.id_edit.text() == doc_id

    # Simulate user changing metadata
    dialog.title_edit.setPlainText("Updated Full Paper Title for Atomic Physics")
    dialog.year_edit.setText("1982")
    dialog.journal_edit.setText("Journal of Physics B")
    dialog.authors_edit.setPlainText("Pyper, Rose, Grant")
    dialog.doi_edit.setText("10.1088/0022-3700/15/9/009")

    # Select BOK type
    dialog.type_combo.setCurrentIndex(2)  # BOK

    dialog._save()

    # Verify db was updated
    updated = db.get_document(doc_id)
    assert updated["title"] == "Updated Full Paper Title for Atomic Physics"
    assert updated["pub_year"] == 1982
    assert updated["journal"] == "Journal of Physics B"
    assert updated["doc_type"] == "BOK"
    assert "Pyper" in updated["authors_list"]
    assert "Rose" in updated["authors_list"]
    assert "Grant" in updated["authors_list"]
    assert updated["doi"] == "10.1088/0022-3700/15/9/009"


def test_metadata_diff_dialog(qapp):
    curr_doc = {
        "doc_id": "DOC-PAP-0000-TEST01",
        "title": "science · 2015 inaccurate title",
        "pub_year": 2015,
        "journal": "science",
        "authors_list": ["Wrong Author"],
        "doi": "",
    }
    from parser.metadata_extractor import DocumentMetadata
    detected = DocumentMetadata(
        title="Accurate Paper Title Discovered by MinerU",
        year=1982,
        journal="Journal of Physics B",
        authors=["Author 1", "Author 2"],
        doi="10.1088/test",
        source="mineru_markdown",
    )

    diff_dlg = MetadataDiffDialog(curr_doc, detected)
    # Verify that fields with differences are checked by default
    assert diff_dlg.checkboxes["title"].isChecked()
    assert diff_dlg.checkboxes["pub_year"].isChecked()
    assert diff_dlg.checkboxes["journal"].isChecked()
    assert diff_dlg.checkboxes["authors"].isChecked()
    assert diff_dlg.checkboxes["doi"].isChecked()

    updates = diff_dlg.get_selected_updates()
    assert updates["doc_id"] == "DOC-PAP-0000-TEST01"
    assert updates["title"] == "Accurate Paper Title Discovered by MinerU"
    assert updates["pub_year"] == 1982
    assert updates["journal"] == "Journal of Physics B"
    assert updates["authors"] == ["Author 1", "Author 2"]
    assert updates["doi"] == "10.1088/test"


def test_library_view_table_not_editable(qapp, tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    db = SQLiteManager(tmp_path / "test_lib.db")
    doc_id = db.upsert_document({
        "title": "Sample Paper Title with Sufficient Length",
        "doc_type": "PAP",
        "pub_year": 2020,
        "journal": "Physical Review A",
    })

    view = LibraryView(db=db, fetcher=MagicMock(), workspace=str(ws))
    # Verify NoEditTriggers
    assert view.table.editTriggers() == QAbstractItemView.NoEditTriggers

    # Populate table and verify items cannot be edited
    view.refresh()
    assert view.table.rowCount() == 1
    for col in range(view.table.columnCount()):
        item = view.table.item(0, col)
        if item is not None:
            # ItemIsEditable (value 4) must NOT be in flags
            assert not (item.flags() & Qt.ItemIsEditable), f"Column {col} has ItemIsEditable flag"
