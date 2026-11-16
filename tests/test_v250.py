import json
import zipfile
from pathlib import Path

import pytest

from parser.mineru_backend import (
    MinerUConfig,
    MinerUConverter,
    MinerUOutputInvalid,
    MinerUResult,
    _blocks_from_structured,
    _pages_from_structured,
    _safe_extract,
)
from parser.pdf_to_markdown import PDFParser
from pipeline import _text_layer_too_sparse


def _make_pdf(path: Path) -> Path:
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "MinerU integration test paper")
    doc.set_metadata({"title": "MinerU test title"})
    doc.save(path)
    doc.close()
    return path


def _structured_payload() -> dict:
    return {
        "metadata": {"document": {"page_count": 2}},
        "pages": [
            {
                "page_idx": 0,
                "blocks": [
                    {"type": "doc_title", "level": 1, "content": "Paper title"},
                    {"type": "text", "content": "First page text."},
                    {"type": "equation", "content": "E = mc^2"},
                ],
            },
            {
                "page_idx": 1,
                "blocks": [
                    {"type": "header", "content": "running header"},
                    {"type": "text", "content": "Second page text."},
                    {
                        "type": "table",
                        "content": "| A | B |\n| --- | --- |\n| 1 | 2 |",
                    },
                ],
            },
        ],
    }


class FakeMinerU:
    enabled = True

    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.calls = 0

    def convert(self, pdf_path, *, doc_id, pdf_sha256=""):
        self.calls += 1
        if self.fail:
            raise RuntimeError("fake MinerU failure")
        return MinerUResult(
            markdown=(
                "<!-- ACDB-PAGE: 1 -->\n# Paper title\n\nFirst page.\n\n"
                "<!-- ACDB-PAGE: 2 -->\n\nSecond page."
            ),
            page_markdown=[
                (1, "# Paper title\n\nFirst page."),
                (2, "Second page."),
            ],
            page_count=2,
            version="4.1.7",
            tier="flash",
            output_dir=Path("cache"),
        )


def test_page_markers_preserve_parser_page_numbers(tmp_path: Path):
    pdf = _make_pdf(tmp_path / "paper.pdf")
    parser = PDFParser(mineru_backend=FakeMinerU())
    parsed = parser.parse(pdf, doc_id="DOC-PAP-2024-TEST")
    assert parsed.metadata["markdown_backend"] == "mineru"
    assert parsed.metadata["mineru_version"] == "4.1.7"
    assert parsed.page_count == 2
    assert any(section.page_start == 1 for section in parsed.sections)
    assert any(section.page_end == 2 for section in parsed.sections)


def test_mineru_failure_falls_back_to_pymupdf(tmp_path: Path):
    pdf = _make_pdf(tmp_path / "paper.pdf")
    parser = PDFParser(mineru_backend=FakeMinerU(fail=True))
    parsed = parser.parse(pdf, doc_id="DOC-PAP-2024-TEST")
    assert parsed.metadata["markdown_backend"] == "pymupdf"
    assert parser.last_mineru_error
    assert parsed.page_count == 1


def test_mineru_success_bypasses_sparse_scan_check(tmp_path: Path):
    pdf = _make_pdf(tmp_path / "paper.pdf")
    parser = PDFParser(mineru_backend=FakeMinerU())
    parsed = parser.parse(pdf, doc_id="DOC-PAP-2024-TEST")
    assert _text_layer_too_sparse(parsed) is False


def test_structured_content_adapter_and_safe_zip(tmp_path: Path):
    payload = _structured_payload()
    zip_path = tmp_path / "out.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("structured_content.json", json.dumps(payload))
        archive.writestr("markdown.md", "# fallback")
    target = tmp_path / "extracted"
    with zipfile.ZipFile(zip_path) as archive:
        _safe_extract(archive, target)
    assert (target / "structured_content.json").is_file()
    pages = _pages_from_structured(payload)
    assert [page for page, _text in pages] == [1, 2]
    assert "E = mc^2" in pages[0][1]
    assert "running header" not in pages[1][1]

    blocks = _blocks_from_structured(payload, doc_id="DOC-PAP-2024-PIPE")
    table = next(block for block in blocks if block["type"] == "table")
    assert table["cells"][0][0]["text"] == "A"
    assert table["cells"][1][1]["text"] == "2"
    assert table["header_rows"] == 1


def test_zip_path_traversal_is_rejected(tmp_path: Path):
    zip_path = tmp_path / "evil.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("../escape.txt", "bad")
    with zipfile.ZipFile(zip_path) as archive:
        with pytest.raises(MinerUOutputInvalid):
            _safe_extract(archive, tmp_path / "out")


def test_converter_caches_fake_cli(tmp_path: Path, monkeypatch):
    payload = _structured_payload()
    fixture = tmp_path / "fixture.zip"
    with zipfile.ZipFile(fixture, "w") as archive:
        archive.writestr("structured_content.json", json.dumps(payload))
        archive.writestr("markdown.md", "# fallback")
    calls = []

    class Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(command, **kwargs):
        calls.append(command)
        output = Path(command[command.index("-o") + 1])
        output.write_bytes(fixture.read_bytes())
        return Completed()

    converter = MinerUConverter(
        tmp_path,
        MinerUConfig(
            command="fake-mineru",
            tier="flash",
            ocr_mode="txt",
            min_markdown_chars=10,
        ),
    )
    monkeypatch.setattr(converter, "_executable", lambda: "fake-mineru")
    monkeypatch.setattr(converter, "version", lambda: "4.1.7")
    monkeypatch.setattr("parser.mineru_backend.subprocess.run", fake_run)
    pdf = _make_pdf(tmp_path / "paper.pdf")
    first = converter.convert(pdf, doc_id="DOC-PAP-2024-TEST")
    second = converter.convert(pdf, doc_id="DOC-PAP-2024-TEST")
    assert first.page_count == 2
    assert second.markdown == first.markdown
    assert len(calls) == 1
    assert first.output_dir.is_dir()


def test_mineru_config_defaults_and_parse_markdown_page_markers():
    cfg = MinerUConfig.from_config({})
    assert cfg.enabled is False
    assert cfg.tier == "standard"
    parser = PDFParser()
    parsed = parser.parse_markdown(
        "<!-- ACDB-PAGE: 3 -->\n# Section\n\nText",
        page_markdown=[(3, "# Section\n\nText")],
        page_count=3,
    )
    assert parsed.sections[-1].page_start == 3


def test_mineru_blocks_keep_page_bbox_and_table_cells():
    from parser.pdf_to_markdown import PDFParser

    payload = {
        "pages": [
            {
                "page_idx": 0,
                "blocks": [
                    {
                        "type": "doc_title",
                        "content": "Paper title",
                        "bbox": [0.1, 0.05, 0.9, 0.12],
                    },
                    {
                        "type": "text",
                        "content": "Body paragraph.",
                        "bbox": [0.1, 0.2, 0.9, 0.3],
                    },
                    {
                        "type": "table",
                        "content": (
                            "<table><tr><th>Model</th><th>n</th></tr>"
                            "<tr><td>B</td><td>8</td></tr></table>"
                        ),
                        "caption": "TABLE 1. Strategy",
                        "bbox": [0.1, 0.4, 0.9, 0.6],
                    },
                    {
                        "type": "image",
                        "image_source": "images/fig1.png",
                        "caption": "Convergence",
                        "bbox": [0.1, 0.7, 0.9, 0.9],
                    },
                ],
            }
        ]
    }
    blocks = _blocks_from_structured(payload, doc_id="DOC-PAP-2024-MINERU")
    assert [block["type"] for block in blocks] == ["doc_title", "text", "table", "image"]
    assert all(block["block_id"].startswith("DOC-PAP-2024-MINERU:p1:b") for block in blocks)
    assert blocks[2]["cells"][0][0]["text"] == "Model"

    parsed = PDFParser().parse_mineru_blocks(
        blocks,
        [(600.0, 800.0)],
        doc_id="DOC-PAP-2024-MINERU",
        page_count=1,
    )
    section = next(section for section in parsed.sections if section.paragraphs)
    assert section.paragraph_blocks[0] is not None
    assert section.paragraph_blocks[0].bbox == (60.0, 160.0, 540.0, 240.0)
    assert section.tables[0].bbox == (60.0, 320.0, 540.0, 480.0)
    assert section.tables[0].cells[0][0]["text"] == "Model"
    assert section.figures[0].caption == "Convergence"

    pipe_blocks = _blocks_from_structured(
        {
            "pages": [
                {
                    "page_idx": 0,
                    "blocks": [
                        {
                            "type": "table",
                            "content": "| A | B |\n| --- | --- |\n| 1 | 2 |",
                            "bbox": [0.1, 0.2, 0.9, 0.4],
                        }
                    ],
                }
            ]
        },
        doc_id="DOC-PAP-2024-PIPE",
    )
    pipe_parsed = PDFParser().parse_mineru_blocks(
        pipe_blocks,
        [(600.0, 800.0)],
        doc_id="DOC-PAP-2024-PIPE",
        page_count=1,
    )
    pipe_table = pipe_parsed.sections[0].tables[0]
    assert pipe_table.source_format == "markdown"
    assert pipe_table.header_rows == 1
    assert pipe_table.cells[1][1]["text"] == "2"
