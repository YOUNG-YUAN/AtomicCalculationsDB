"""Unit tests for v3.1.0:
- BibTeX import and metadata extraction (parse_bibtex_text, parse_bibtex_file, bibtex_to_metadata)
- BibtexImportDialog for file selection or text pasting
- DocumentEditDialog with read-only DOC-ID and full field modification
- MetadataDiffDialog for smart diff comparison and selective calibration
- LibraryView table edit triggers set to NoEditTriggers to prevent accidental cell editing on detail close
- Version check for v3.1.0
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView, QApplication, QDialog

from db.sqlite_manager import SQLiteManager
from parser.metadata_extractor import (
    bibtex_to_metadata,
    parse_bibtex_file,
    parse_bibtex_text,
)
from ui.dialogs import BibtexImportDialog, DocumentEditDialog, MetadataDiffDialog
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


def test_parse_bibtex_text():
    sample_bib = """
    @article{pyper1982analysis,
      title={Analysis of fine-structure excitation energies in Dirac-Fock and perturbation theories},
      author={Pyper, N. C. and Rose, S. J. and Grant, I. P.},
      journal={Journal of Physics B: Atomic and Molecular Physics},
      volume={15},
      number={9},
      pages={1319},
      year={1982},
      publisher={IOP Publishing},
      doi={10.1088/0022-3700/15/9/009}
    }

    @book{grant2007relativistic,
      title = "Relativistic Quantum Theory of Atoms and Molecules: Theory and Computation",
      author = "Grant, Ian P.",
      year = "2007",
      publisher = "Springer Science & Business Media",
      isbn = "9780387346717"
    }

    @manual{mcdf_manual,
      title = {GRASP2K User Manual},
      author = {Jonsson, P. and Gaigalas, G.},
      year = {2013}
    }
    """
    entries = parse_bibtex_text(sample_bib)
    assert len(entries) == 3

    # Entry 1: Article (PAP)
    e1 = entries[0]
    assert e1["cite_key"] == "pyper1982analysis"
    assert e1["doc_type"] == "PAP"
    assert e1["title"] == "Analysis of fine-structure excitation energies in Dirac-Fock and perturbation theories"
    assert e1["pub_year"] == 1982
    assert e1["journal"] == "Journal of Physics B: Atomic and Molecular Physics"
    assert e1["doi"] == "10.1088/0022-3700/15/9/009"
    assert e1["authors"] == ["N. C. Pyper", "S. J. Rose", "I. P. Grant"]

    # Entry 2: Book (BOK)
    e2 = entries[1]
    assert e2["cite_key"] == "grant2007relativistic"
    assert e2["doc_type"] == "BOK"
    assert e2["title"] == "Relativistic Quantum Theory of Atoms and Molecules: Theory and Computation"
    assert e2["pub_year"] == 2007
    assert e2["authors"] == ["Ian P. Grant"]

    # Entry 3: Manual (MAN)
    e3 = entries[2]
    assert e3["doc_type"] == "MAN"
    assert e3["title"] == "GRASP2K User Manual"
    assert e3["pub_year"] == 2013


def test_parse_bibtex_file(tmp_path):
    bib_file = tmp_path / "references.bib"
    bib_file.write_text(
        "@article{test2024,\n"
        "  title = {Atomic Physics in BibTeX},\n"
        "  author = {Smith, John and Doe, Jane},\n"
        "  journal = {Physical Review A},\n"
        "  year = {2024},\n"
        "  doi = {https://doi.org/10.1103/PhysRevA.100.012345}\n"
        "}\n",
        encoding="utf-8",
    )
    entries = parse_bibtex_file(bib_file)
    assert len(entries) == 1
    assert entries[0]["title"] == "Atomic Physics in BibTeX"
    assert entries[0]["authors"] == ["John Smith", "Jane Doe"]
    assert entries[0]["pub_year"] == 2024
    assert entries[0]["doi"] == "10.1103/PhysRevA.100.012345"


def test_bibtex_to_metadata():
    entry = {
        "title": "Sample BibTeX Title",
        "authors": ["Author One", "Author Two"],
        "journal": "Nature Physics",
        "pub_year": 2023,
        "doi": "10.1038/s41567-023-0001",
    }
    meta = bibtex_to_metadata(entry)
    assert meta.title == "Sample BibTeX Title"
    assert meta.authors == ["Author One", "Author Two"]
    assert meta.journal == "Nature Physics"
    assert meta.year == 2023
    assert meta.doi == "10.1038/s41567-023-0001"
    assert meta.source == "bibtex"
    assert meta.confidence == 1.0


def test_bibtex_import_dialog(qapp, tmp_path):
    db = SQLiteManager(tmp_path / "test_bib_import.db")
    doc_id = db.upsert_document({
        "title": "science · 2015 inaccurate title",
        "doc_type": "PAP",
        "pub_year": 2015,
        "journal": "science",
        "authors": ["Unknown"],
    })
    doc = db.get_document(doc_id)
    assert doc is not None

    dlg = BibtexImportDialog(db, doc)
    sample_bib = """
    @article{correct2024,
      title = {Accurate Fine Structure Computation in Relativistic Atoms},
      author = {Pyper, N. C. and Grant, I. P.},
      journal = {Journal of Physics B},
      year = {1982},
      doi = {10.1088/0022-3700/15/9/009}
    }
    """
    dlg.text_edit.setPlainText(sample_bib)

    # Mock diff dialog to auto-accept
    with patch("ui.dialogs.MetadataDiffDialog.exec", return_value=QDialog.Accepted):
        dlg._parse_and_update()

    # Verify db updated
    updated = db.get_document(doc_id)
    assert updated["title"] == "Accurate Fine Structure Computation in Relativistic Atoms"
    assert updated["pub_year"] == 1982
    assert updated["journal"] == "Journal of Physics B"
    assert updated["doi"] == "10.1088/0022-3700/15/9/009"
    assert "N. C. Pyper" in updated["authors_list"]


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
        title="Accurate Paper Title Discovered by BibTeX",
        year=1982,
        journal="Journal of Physics B",
        authors=["Author 1", "Author 2"],
        doi="10.1088/test",
        source="bibtex",
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
    assert updates["title"] == "Accurate Paper Title Discovered by BibTeX"
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
