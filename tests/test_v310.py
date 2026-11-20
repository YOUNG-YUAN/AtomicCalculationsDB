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


def test_agent_model_last_used_persistence(qapp, tmp_path):
    from config import AppConfig
    from ui.semi_auto_dialog import (
        DEFAULT_AGENT_MODEL,
        PasteReplyDialog,
        SemiAutoBatchDialog,
        get_default_agent_model,
        save_default_agent_model,
    )

    # 1. Initial state for fresh config is GPT-6.2 Sol Ultra
    cfg = AppConfig({}, tmp_path / "config.yaml")
    assert get_default_agent_model(cfg) == "GPT-6.2 Sol Ultra"
    assert DEFAULT_AGENT_MODEL == "GPT-6.2 Sol Ultra"

    # 2. Save a custom model and verify get_default_agent_model retrieves it
    save_default_agent_model("Claude 3.7 Sonnet", cfg)
    assert get_default_agent_model(cfg) == "Claude 3.7 Sonnet"
    assert cfg.get("semi_auto.agent_model") == "Claude 3.7 Sonnet"

    # 3. Verify SemiAutoBatchDialog initializes with this model
    db = SQLiteManager(tmp_path / "test_semi.db")
    doc_id = db.upsert_document({"title": "Test Paper for Semi Auto", "doc_type": "PAP", "pub_year": 2024})
    dlg = SemiAutoBatchDialog(db, tmp_path, pipeline=None, doc_ids=[doc_id], config=cfg)
    assert dlg.model_combo.currentText() == "Claude 3.7 Sonnet"
    assert dlg.current_agent_model == "Claude 3.7 Sonnet"

    # 4. User changes model in combo box -> automatically persisted
    dlg.model_combo.setCurrentText("DeepSeek R1")
    assert cfg.get("semi_auto.agent_model") == "DeepSeek R1"
    assert get_default_agent_model(cfg) == "DeepSeek R1"

    # 5. Verify PasteReplyDialog also picks up the updated default model
    paste_dlg = PasteReplyDialog(1, config=cfg)
    assert paste_dlg.model_combo.currentText() == "DeepSeek R1"
    assert paste_dlg.get_model_name() == "DeepSeek R1"

    # 6. Reset to GPT-6.2 Sol Ultra
    save_default_agent_model("GPT-6.2 Sol Ultra", cfg)
    assert get_default_agent_model(cfg) == "GPT-6.2 Sol Ultra"


def test_skeletonize_table_markdown():
    from parser.table_compressor import skeletonize_table_markdown

    # 1. Short table remains intact
    short_table = (
        "| State | Energy (cm-1) |\n"
        "| --- | --- |\n"
        "| 2p5 3s 3P0 | 12345.6 |\n"
        "| 2p5 3s 3P1 | 12567.8 |"
    )
    assert skeletonize_table_markdown(short_table).strip() == short_table

    # 2. Giant 50-row table is skeletonized to top 5 rows + omission marker
    rows = [f"| 2p5 3d {i} | {10000 + i * 100:.1f} |" for i in range(50)]
    giant_table = (
        "| State | Energy (cm-1) |\n"
        "| --- | --- |\n"
        + "\n".join(rows)
    )
    skeleton = skeletonize_table_markdown(giant_table, max_sample_rows=5, caption="Table 1. Energy levels")
    assert "Table 1. Energy levels" in skeleton
    assert "2p5 3d 0" in skeleton
    assert "2p5 3d 4" in skeleton
    assert "2p5 3d 10" not in skeleton
    assert "此处省略 45 行常规数值" in skeleton


def test_table_on_demand_and_pseudo_conclusion_filtering():
    from parser.pdf_to_markdown import ParsedDocument, Section, TableBlock
    from parser.four_channel_radar import FourChannelRadar
    from parser.context_bundler import ContextBundler

    # Construct a synthetic document:
    # Section 1 has theoretical description without table reference
    # Section 2 has a conclusion that refers to Table 1
    # Section 3 is "Explanation of Tables" with continuation markers that should be skipped
    sec1 = Section(
        title="1. Theoretical Method",
        level=1,
        path="Paper > 1. Theoretical Method",
        page_start=1,
        page_end=2,
        paragraphs=[
            "The relativistic Dirac-Coulomb Hamiltonian is used to generate wavefunctions.",
            "We found that core-valence correlation contributes significantly to the excitation energy.",
        ],
    )
    # A 100-row table in Section 2
    table1_rows = [f"| Level {i} | {20000 + i * 50} |" for i in range(100)]
    table1_md = "| Level | Energy |\n| --- | --- |\n" + "\n".join(table1_rows)
    sec2 = Section(
        title="2. Results and Discussion",
        level=1,
        path="Paper > 2. Results and Discussion",
        page_start=3,
        page_end=4,
        paragraphs=[
            "As demonstrated in Table 1, the calculated fine-structure intervals agree with experiment within 0.1%.",
        ],
        tables=[
            TableBlock(
                block_id="tbl-1",
                caption="Table 1. Energy levels and fine-structure splittings.",
                markdown=table1_md,
                rows=100,
            )
        ],
    )
    sec3_appendix = Section(
        title="Explanation of Tables",
        level=1,
        path="Paper > Explanation of Tables",
        page_start=5,
        page_end=50,
        paragraphs=[
            "{'bbox': [10, 20, 30, 40], 'content': 'Table 1 Energy levels.'}",
            "Table 1 (continued)",
            "Table 2 (continued)",
        ],
        tables=[
            TableBlock(
                block_id="tbl-1-cont",
                caption="Table 1 (continued)",
                markdown=table1_md,
                rows=100,
            )
        ],
    )

    doc = ParsedDocument(
        title="Spectroscopic Data for Ne-like Ions",
        sections=[sec1, sec2, sec3_appendix],
        page_count=50,
        doc_id="DOC-TEST-0001",
    )

    radar = FourChannelRadar().scan(doc)
    bundler = ContextBundler()
    bundles = bundler.build(doc, radar, doc_id=doc.doc_id)

    # 1. Non-conclusion section "Explanation of Tables" and bare continuation markers produced 0 bundles!
    sec_names = [b.section_path for b in bundles]
    assert not any("Explanation of Tables" in s for s in sec_names)

    # 2. Table-on-Demand: Section 1 items did NOT reference any table -> table_markdown is empty
    sec1_bundles = [b for b in bundles if "1. Theoretical Method" in b.section_path]
    assert len(sec1_bundles) >= 1
    for b in sec1_bundles:
        assert b.table_markdown == ""

    # 3. Section 2 referenced Table 1 -> table_markdown is attached and skeletonized (not 100 rows!)
    sec2_bundles = [b for b in bundles if "2. Results and Discussion" in b.section_path]
    assert len(sec2_bundles) >= 1
    assert sec2_bundles[0].table_markdown != ""
    assert "Table 1" in sec2_bundles[0].table_markdown
    assert "此处省略" in sec2_bundles[0].table_markdown
    assert "Level 99" not in sec2_bundles[0].table_markdown


def test_restructured_question_packets():
    from parser.context_bundler import ContextBundle
    from parser.batch_packer import DistilledItem
    from models.endpoint_pool import EndpointNode
    from models.web_ai_bridge import ClipboardBatchService

    b1 = ContextBundle(
        doc_id="DOC-01",
        section_path="1. Methods",
        focal_window_markdown="Dirac-Coulomb Hamiltonian formulation.",
        focal_sentence="Hamiltonian formulation.",
        table_markdown="",  # No table called
    )
    b2 = ContextBundle(
        doc_id="DOC-01",
        section_path="2. Results",
        focal_window_markdown="As shown in Table 1, agreement is excellent.",
        focal_sentence="Agreement is excellent.",
        table_markdown="| Level | Energy |\n| --- | --- |\n| 1 | 100 |\n| 2 | 200 |\n",
    )

    items = [
        DistilledItem(
            item_id="item-1",
            text=b1.prompt_markdown(),
            metadata={"doc_id": "DOC-01", "bundle": b1.model_dump(mode="json")},
        ),
        DistilledItem(
            item_id="item-2",
            text=b2.prompt_markdown(),
            metadata={"doc_id": "DOC-01", "bundle": b2.model_dump(mode="json")},
        ),
    ]

    node = EndpointNode(
        node_id="test-node",
        name="Test Node",
        node_type="OPENAI_COMPATIBLE_API",
        capabilities=["TEXT_LLM"],
        max_input_tokens=22000,
        max_output_tokens=8000,
    )

    service = ClipboardBatchService()
    packets = service.build_packets(items, node, system_prompt="SYSTEM PROMPT INSTRUCTION")
    assert len(packets) == 1
    prompt = packets[0].prompt

    # Verify 3-part restructured prompt:
    # 1. Format and output declaration block at top
    assert "【任务与输出规范】" in prompt
    assert "SYSTEM PROMPT INSTRUCTION" in prompt
    assert "【输出要求】" in prompt
    assert "LaTeX 反斜杠必须写为双反斜杠" in prompt

    # 2. Specific question items block
    assert "【具体提问条目】" in prompt
    assert "### ITEM item-1" in prompt
    assert "### ITEM item-2" in prompt

    # 3. On-demand table attachments block at bottom
    assert "【参考表格附件 (按需调用)】" in prompt
    assert "[表格附件 1]" in prompt
    assert "| Level | Energy |" in prompt


def test_agent_task_packager_export_and_submit(tmp_path):
    from db.sqlite_manager import SQLiteManager
    from parser.context_bundler import ContextBundle
    from sync_mcp.agent_task_packager import AgentTaskPackager
    from models.atomic_prompts import ExtractionResult, KnowledgeCardDraft

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = SQLiteManager(workspace)

    doc_id = "DOC-PAP-2024-A1B2C3"
    storage_dir = workspace / "storage" / doc_id
    storage_dir.mkdir(parents=True)
    pdf_file = storage_dir / "paper.pdf"
    pdf_file.write_bytes(b"%PDF-1.4 test")

    parsed_md = storage_dir / "parsed.md"
    parsed_md.write_text("# Test Paper\n\nDiscussion on Dirac-Fock calculations in Table 1.", encoding="utf-8")

    parsed_tables = storage_dir / "parsed_tables.json"
    parsed_tables.write_text(
        json.dumps([
            {"caption": "Table 1. Calculated energy levels.", "markdown": "| Level | Energy |\n| --- | --- |\n| 1 | 0.0 |\n| 2 | 10.5 |\n"}
        ]),
        encoding="utf-8"
    )

    bundle = ContextBundle(
        doc_id=doc_id,
        section_path="Methods > Dirac-Fock",
        focal_sentence="We report energies in Table 1.",
        focal_window_markdown="As detailed in Table 1, the energies are calculated using Dirac-Fock.",
    )
    bundles_file = storage_dir / "bundles.json"
    bundles_file.write_text(json.dumps([bundle.model_dump(mode="json")]), encoding="utf-8")

    db.upsert_document({
        "doc_id": doc_id,
        "title": "Test Atomic Paper for Agent",
        "doc_type": "PAP",
        "pub_year": 2024,
        "local_pdf_path": str(pdf_file.relative_to(workspace)),
        "parse_status": "LIGHT_PARSED",
    })

    pipeline = MagicMock()
    pipeline._store_draft.return_value = "KNW-TEST-001"

    packager = AgentTaskPackager(db, workspace, pipeline=pipeline)

    # 1. Export
    export_res = packager.export_task_bundle(doc_id)
    assert export_res["ok"] is True
    assert export_res["doc_id"] == doc_id
    assert export_res["item_count"] == 1
    assert export_res["table_count"] == 1

    task_dir = workspace / "agent_tasks" / doc_id
    assert task_dir.is_dir()
    assert (task_dir / "task_instructions.md").is_file()
    assert (task_dir / "items.json").is_file()
    assert (task_dir / "schema.json").is_file()
    assert (task_dir / "parsed.md").is_file()
    assert (task_dir / "tables" / "Table_1.md").is_file()

    # Verify instructions and items content
    instr = (task_dir / "task_instructions.md").read_text(encoding="utf-8")
    assert doc_id in instr
    assert "Test Atomic Paper for Agent" in instr
    assert "tables/Table_1.md" in instr

    items_data = json.loads((task_dir / "items.json").read_text(encoding="utf-8"))
    assert len(items_data) == 1
    assert items_data[0]["item_id"] == f"{doc_id}-0"
    assert items_data[0]["table_refs"] == ["1"]
    assert items_data[0]["table_files"] == ["tables/Table_1.md"]

    # 2. Check Status
    status_res = packager.get_task_status(doc_id)
    assert status_res["ok"] is True
    assert status_res["has_task_bundle"] is True
    assert status_res["item_count"] == 1
    assert status_res["has_result_json"] is False

    # 3. Submit Result
    card_draft = KnowledgeCardDraft(
        card_type="MODEL",
        title_zh="Dirac-Fock 方法与能级计算",
        summary_zh="计算采用相对论 Dirac-Fock 方法，能量为 10.5 eV",
        premises_and_conditions="适用于类氖离子基态与激发态能级计算",
        confidence=0.95,
        element="Fe",
        ion_stage="Fe XVII",
        method="Dirac-Fock",
        code_name="GRASP",
    )
    result_payload = {
        "items": [
            {
                "item_id": f"{doc_id}-0",
                "result": {
                    "skip": False,
                    "cards": [card_draft.model_dump(mode="json")],
                    "keywords": ["Dirac-Fock", "Fe XVII"],
                }
            }
        ]
    }

    submit_res = packager.submit_task_result(
        doc_id,
        result_payload,
        agent_name="CodexAgent",
        agent_model="gpt-5-codex",
    )
    assert submit_res["ok"] is True
    assert submit_res["processed_items"] == 1
    assert submit_res["stored_cards"] == 1
    assert submit_res["stored_keywords"] == 2
    assert submit_res["skipped_items"] == 0

    # Verify pipeline was called
    pipeline._store_draft.assert_called_once()

    # Verify doc status in DB is now LLM_EXTRACTED
    doc_after = db.get_document(doc_id)
    assert doc_after["parse_status"] == "LLM_EXTRACTED"


def test_agent_workbench_and_cli_dispatch(tmp_path):
    from db.sqlite_manager import SQLiteManager
    from sync_mcp.agent_workbench import AgentWorkbenchService
    from main_entry import _parse_cli, _run_agent_cli
    from config import AppConfig

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = AppConfig(path=workspace / "config.yaml")
    config.set("mcp.allow_task_write", True)
    config.save()

    doc_id = "DOC-PAP-2023-B2C3D4"
    db = SQLiteManager(workspace)
    db.upsert_document({
        "doc_id": doc_id,
        "title": "CLI Test Document",
        "doc_type": "PAP",
        "pub_year": 2023,
    })

    wb = AgentWorkbenchService(db, workspace)
    status = wb.get_agent_task_status(doc_id)
    assert status["ok"] is True
    assert status["doc_id"] == doc_id
    assert status["has_task_bundle"] is False

    # CLI export test
    args = _parse_cli(["main.py", "agent", "status", doc_id, "--workspace", str(workspace)])
    assert args.subcommand == "agent"
    assert args.agent_action == "status"
    assert args.doc_id == doc_id

    code = _run_agent_cli(tmp_path, config, args)
    assert code == 0


def test_rate_limit_detection_and_execute_resilience(tmp_path):
    from sync_mcp.agent_task_packager import is_rate_limit_or_timeout_error, AgentTaskPackager
    from db.sqlite_manager import SQLiteManager
    from parser.context_bundler import ContextBundle
    from models.atomic_prompts import KnowledgeCardDraft

    # 1. Test error classification
    assert is_rate_limit_or_timeout_error(RuntimeError("HTTP 429 Too Many Requests"))
    assert is_rate_limit_or_timeout_error(Exception("Rate limit reached for model gpt-4o"))
    assert is_rate_limit_or_timeout_error(Exception("TPM limit exceeded, please wait 300s"))
    assert is_rate_limit_or_timeout_error(TimeoutError("Connection timed out"))
    assert not is_rate_limit_or_timeout_error(ValueError("Invalid syntax"))

    # 2. Test execute resilience with auto-pause & polling
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = SQLiteManager(workspace)

    doc_id = "DOC-PAP-2024-C3D4E5"
    storage_dir = workspace / "storage" / doc_id
    storage_dir.mkdir(parents=True)
    pdf_file = storage_dir / "paper.pdf"
    pdf_file.write_bytes(b"%PDF-1.4 test")

    parsed_md = storage_dir / "parsed.md"
    parsed_md.write_text("# Paper\n\nMethod discussion.", encoding="utf-8")

    bundle1 = ContextBundle(doc_id=doc_id, section_path="Sec 1", focal_sentence="Line 1.")
    bundle2 = ContextBundle(doc_id=doc_id, section_path="Sec 2", focal_sentence="Line 2.")
    bundles_file = storage_dir / "bundles.json"
    bundles_file.write_text(
        json.dumps([bundle1.model_dump(mode="json"), bundle2.model_dump(mode="json")]),
        encoding="utf-8",
    )

    db.upsert_document({
        "doc_id": doc_id,
        "title": "Resilience Test Paper",
        "doc_type": "PAP",
        "pub_year": 2024,
        "local_pdf_path": str(pdf_file.relative_to(workspace)),
        "parse_status": "LIGHT_PARSED",
    })

    pipeline = MagicMock()
    pipeline._store_draft.return_value = "KNW-TEST-RES"
    packager = AgentTaskPackager(db, workspace, pipeline=pipeline)

    # Mock extractor that simulates a 429 rate limit on first attempt, then succeeds
    attempts = {"count": 0}
    card_draft = KnowledgeCardDraft(
        card_type="MODEL",
        title_zh="测试卡片",
        summary_zh="测试内容",
        premises_and_conditions="全温全压",
    )

    def mock_extractor(bundle):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("HTTP 429: Rate limit reached, please retry later")
        return {
            "skip": False,
            "cards": [card_draft.model_dump(mode="json")],
            "keywords": [{"term_en": "Relativity"}],
        }

    # Execute with short poll interval (1s) to test pause & recovery
    res = packager.execute_task_bundle(
        doc_id,
        poll_interval_sec=1,
        custom_extractor=mock_extractor,
        auto_submit=True,
    )
    assert res["ok"] is True
    assert res["total_items"] == 2
    assert res["completed_items"] == 2
    assert res["is_complete"] is True
    assert attempts["count"] >= 3  # 1 failure + 2 successes

    # Verify task instructions contains rate limit section
    task_dir = workspace / "agent_tasks" / doc_id
    instr = (task_dir / "task_instructions.md").read_text(encoding="utf-8")
    assert "限额、超时与断点续传弹性规则" in instr
    assert "绝对禁止因限额或超时中断任务" in instr



