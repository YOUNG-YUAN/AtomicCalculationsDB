"""P1 table ingestion and structured applicability conditions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from audit.importer import apply_audit_payload, preview_audit_payload
from audit.report import export_audit_bundle
from db.card_hash import compute_card_content_hash
from db.sqlite_manager import SQLiteManager
from models.atomic_prompts import ExtractionResult, KnowledgeCardDraft
from parser.context_bundler import ContextBundle, ContextBundler
from parser.four_channel_radar import FourChannelRadar
from parser.html_table import html_table_cells, html_table_to_markdown
from parser.pdf_to_markdown import PDFParser
from pipeline import Pipeline


def _db(tmp_path: Path) -> SQLiteManager:
    return SQLiteManager(tmp_path / "workspace", embedding_signature="test::dim=4")


def test_html_table_preserves_spans_and_flattens_for_radar():
    html = (
        '<table><tr><th>Model</th><th>Active space</th></tr>'
        '<tr><td rowspan="2">B</td><td>n=7</td></tr>'
        '<tr><td>n=8</td></tr></table>'
    )
    markdown = html_table_to_markdown(html)
    assert "| Model | Active space |" in markdown
    assert markdown.count("| B |") == 2
    cells = html_table_cells(html)
    assert cells[1][0]["rowspan"] == 2
    assert cells[1][0]["text"] == "B"

    parsed = PDFParser().parse_markdown(
        "# Paper\n\nTABLE 1. Strategy\n\n" + html + "\n\nAfter.",
        page_count=1,
    )
    section = next(section for section in parsed.sections if section.tables)
    table = section.tables[0]
    assert table.source_format == "html"
    assert table.html == html
    assert "| Model | Active space |" in table.markdown
    assert table.cells[1][0]["rowspan"] == 2
    assert "<table" not in parsed.markdown
    assert "| Model | Active space |" in parsed.markdown

    radar = FourChannelRadar().scan(parsed)
    assert any(hit.channel == "STRATEGY_TABLE" for hit in radar.hits)
    bundles = ContextBundler().build(parsed, radar, doc_id="DOC-PAP-2024-TABLE")
    table_bundle = next(
        bundle for bundle in bundles if "STRATEGY_TABLE" in bundle.detected_channels
    )
    assert table_bundle.locator["table_cells"][0][0]["text"] == "Model"
    assert table_bundle.locator["table_header_rows"] == 1


def test_conditions_are_stored_and_rendered_in_audit_report(tmp_path: Path):
    db = _db(tmp_path)
    try:
        doc = db.upsert_document({"title": "Condition paper", "pub_year": 2024})

        class Gateway:
            def extract_cards(self, bundle, *, doc_metadata=None, manual_mode=False):
                return ExtractionResult(
                    cards=[
                        KnowledgeCardDraft(
                            card_type="STRAT",
                            title_zh="条件测试",
                            summary_zh="扩大活性空间。",
                            premises_and_conditions="基态。",
                            conditions={
                                "target_quantity": "energy",
                                "unit": "cm^-1",
                                "value": "123.4",
                                "uncertainty": "0.2",
                                "transition": "E1",
                                "unknown_fields": ["j_parity"],
                            },
                        )
                    ]
                )

        bundle = ContextBundle(
            doc_id=doc,
            section_path="1. Method",
            focal_window_markdown="⟪FOCUS⟫ condition test ⟪/FOCUS⟫",
            focal_sentence="condition test",
        )
        pipeline = Pipeline(db, tmp_path / "workspace", gateway=Gateway())
        pipeline.extract_bundles(doc, [bundle])

        card = db.list_knowledge_cards(limit=10)[0]
        detail = db.get_knowledge_card(card["knw_id"])
        conditions = detail["structured"]["conditions"]
        assert conditions["target_quantity"] == "energy"
        assert conditions["unit"] == "cm^-1"
        assert conditions["unknown_fields"] == ["j_parity"]

        result = export_audit_bundle(db, tmp_path / "workspace", doc)
        report = (result.export_dir / "report.md").read_text(encoding="utf-8")
        assert "结构化适用条件" in report
        assert "目标量" in report
        assert "unknown_fields" in report or "j_parity" in report
    finally:
        db.close()


def test_audit_revision_can_update_conditions(tmp_path: Path):
    db = _db(tmp_path)
    try:
        doc = db.upsert_document({"title": "Condition revision", "pub_year": 2024})
        card_id = db.upsert_knowledge_card(
            {
                "card_type": "STRAT",
                "title_zh": "条件修订",
                "summary_zh": "原始结论。",
                "premises_and_conditions": "原始前提。",
                "structured_details_json": {
                    "conditions": {"target_quantity": "energy"}
                },
            }
        )
        exported = export_audit_bundle(db, tmp_path / "workspace", doc)
        card = db.get_knowledge_card(card_id)
        payload = {
            "schema": "acdb.audit.v1",
            "document_id": doc,
            "report_id": exported.report_id,
            "report_hash": exported.report_hash,
            "batch_id": "part-01",
            "auditor": {"name": "外部 AI"},
            "card_reviews": [
                {
                    "knw_id": card_id,
                    "content_hash": compute_card_content_hash(card),
                    "overall_score": 80,
                    "verdict": "基本相符",
                    "suggested_action": "modify",
                    "issues": [],
                    "revised_card": {
                        "conditions": {"target_quantity": "transition rate"}
                    },
                }
            ],
            "new_cards": [],
        }
        preview = preview_audit_payload(db, payload)
        apply_audit_payload(db, preview, selected_knw_ids=[card_id])
        updated = db.get_knowledge_card(card_id)
        assert updated["structured"]["conditions"]["target_quantity"] == "transition rate"
    finally:
        db.close()


def test_evidence_locator_roundtrip(tmp_path: Path):
    db = _db(tmp_path)
    try:
        doc = db.upsert_document({"title": "Locator paper"})
        card_id = db.upsert_knowledge_card(
            {
                "card_type": "CONC",
                "title_zh": "定位测试",
                "summary_zh": "证据定位。",
                "premises_and_conditions": "测试。",
            }
        )
        db.add_evidence(
            card_id,
            doc,
            "⟪FOCUS⟫ evidence locator ⟪/FOCUS⟫",
            section_title="2. Method",
            page_num=4,
            locator={
                "page_num": 4,
                "section_path": "2. Method",
                "paragraph_index": 7,
                "pdf_sha256": "abc",
            },
        )
        evidence = db.list_evidence(card_id)[0]
        assert evidence["locator"]["page_num"] == 4
        assert evidence["locator"]["paragraph_index"] == 7
        assert evidence["locator"]["pdf_sha256"] == "abc"
    finally:
        db.close()


def test_figure_caption_reaches_radar():
    parsed = PDFParser().parse_markdown(
        "# Results\n\n![Convergence of Model B active-space scan](fig1.png)\n",
        page_count=1,
    )
    section = parsed.sections[-1]
    assert len(section.figures) == 1
    assert section.figures[0].caption.startswith("Convergence of Model B")
    radar = FourChannelRadar().scan(parsed)
    assert any(hit.channel == "FIGURE" for hit in radar.hits)


def test_parse_writes_tables_figures_and_manifest(tmp_path: Path):
    from parser.pdf_to_markdown import (
        FigureBlock,
        ParsedDocument,
        Section,
        TableBlock,
    )

    class FakeParser:
        def parse(self, path, *, doc_id=None, title=None):
            section = Section(title="Results", level=1, path="Results")
            section.paragraphs = ["Results paragraph."]
            section.tables = [
                TableBlock(
                    caption="TABLE 1. Strategy",
                    markdown="| Model | n |\n| --- | --- |\n| B | 8 |",
                    rows=1,
                    page_num=1,
                    source_format="html",
                    html="<table><tr><td>Model</td></tr></table>",
                    cells=[[{"text": "Model", "header": True}]],
                )
            ]
            section.figures = [
                FigureBlock(caption="Convergence", image="fig1.png", page_num=1)
            ]
            return ParsedDocument(
                title="Manifest paper",
                markdown="# Results\n\nResults paragraph.",
                sections=[section],
                page_count=1,
            )

    db = _db(tmp_path)
    try:
        pdf = tmp_path / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        doc = db.upsert_document(
            {"title": "Manifest paper", "local_pdf_path": str(pdf)}
        )
        pipeline = Pipeline(
            db, tmp_path / "workspace", parser=FakeParser()
        )
        result = pipeline.parse_document(doc)
        assert result is not None
        directory = pdf.parent
        tables = json.loads((directory / "parsed_tables.json").read_text(encoding="utf-8"))
        figures = json.loads((directory / "parsed_figures.json").read_text(encoding="utf-8"))
        manifest = json.loads((directory / "parse_manifest.json").read_text(encoding="utf-8"))
        assert tables[0]["source_format"] == "html"
        assert tables[0]["cells"][0][0]["text"] == "Model"
        assert figures[0]["caption"] == "Convergence"
        assert manifest["table_count"] == 1
        assert manifest["figure_count"] == 1
    finally:
        db.close()


def test_pymupdf_block_locator_has_bbox_and_char_offsets(tmp_path: Path):
    fitz = pytest.importorskip("pymupdf")
    pdf = tmp_path / "locator.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "1. Methods", fontsize=16)
    page.insert_text((72, 110), "We show that the method is sufficient.", fontsize=11)
    document.save(pdf)
    document.close()

    from parser.context_bundler import ContextBundler
    from parser.four_channel_radar import FourChannelRadar

    parser = PDFParser()
    parsed = parser.parse(pdf, doc_id="DOC-PAP-2024-LOCATE")
    section = next(section for section in parsed.sections if section.paragraphs)
    index = 0
    block = section.paragraph_blocks[index]
    assert block is not None
    assert block.block_id.startswith("DOC-PAP-2024-LOCATE:p")
    assert block.bbox is not None
    assert block.char_end == len(block.text)

    radar = FourChannelRadar().scan(parsed)
    bundles = ContextBundler().build(parsed, radar, doc_id="DOC-PAP-2024-LOCATE")
    assert bundles
    locator = bundles[0].locator
    assert locator["block_id"].startswith("DOC-PAP-2024-LOCATE:p")
    assert locator["bbox"] is not None
    assert locator["char_end"] >= locator["char_start"]


def test_pipe_table_cells_are_structured():
    parsed = PDFParser().parse_markdown(
        "# Paper\n\n| A | B |\n|---|---|\n| 1 | 2 |\n",
        page_count=1,
    )
    table = next(section for section in parsed.sections if section.tables).tables[0]
    assert table.cells[0][0]["text"] == "A"
    assert table.cells[1][1]["text"] == "2"
    assert table.header_rows == 1


def test_pymupdf_table_cells_have_bboxes(tmp_path: Path):
    fitz = pytest.importorskip("pymupdf")
    pdf = tmp_path / "table.pdf"
    document = fitz.open()
    page = document.new_page()
    for y in (100, 130, 160):
        page.draw_line((72, y), (400, y))
    for x in (72, 200, 400):
        page.draw_line((x, 100), (x, 160))
    page.insert_text((80, 120), "A")
    page.insert_text((210, 120), "B")
    page.insert_text((80, 150), "1")
    page.insert_text((210, 150), "2")
    document.save(pdf)
    document.close()

    parsed = PDFParser().parse(pdf, doc_id="DOC-PAP-2024-TABLECELL")
    section = next(section for section in parsed.sections if section.tables)
    table = section.tables[0]
    assert table.source_format == "pymupdf"
    assert table.cells[0][0]["bbox"] is not None
    assert table.cells[0][0]["text"] == "A"
    assert table.cells[1][1]["text"] == "2"


def test_pdf_locator_dialog_renders_page_and_bbox(tmp_path: Path, qapp):
    import pymupdf as fitz
    from ui.pdf_locator import PdfLocatorDialog

    pdf = tmp_path / "locator-view.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "Locator target", fontsize=12)
    document.save(pdf)
    document.close()

    dialog = PdfLocatorDialog(
        pdf,
        {"page_num": 1, "bbox": [60, 50, 300, 100], "block_id": "test:p1:b000"},
    )
    assert dialog.windowTitle() == "原文定位"
    dialog.close()
