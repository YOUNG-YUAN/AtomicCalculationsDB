"""CPU-only PDF → structured Markdown.

Uses PyMuPDF (``fitz``) for fast block extraction.  The output preserves a
section tree, page numbers, tables, formula-looking blocks and code/command
blocks so the four-channel radar and the three-point context bundler can work
without sending the whole PDF to an LLM.

Optional external routes (MinerU / VLM / RapidLatexOCR) are represented by a
small backend protocol; when unavailable the local PyMuPDF path is used.
"""

from __future__ import annotations

import logging
import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping, Protocol

from parser.html_table import (
    html_table_cells,
    html_table_to_markdown,
    markdown_table_cells,
    table_header_rows,
)
from parser.metadata_extractor import (
    DocumentMetadata,
    extract_document_metadata,
    is_valid_title,
)
from parser.mineru_backend import MinerUConverter, MinerUError

log = logging.getLogger(__name__)

try:  # PyMuPDF ≥1.24 exports the modern name
    import pymupdf as fitz  # type: ignore
except ImportError:  # pragma: no cover - older wheels
    try:
        import fitz  # type: ignore
    except ImportError:  # pragma: no cover
        fitz = None  # type: ignore


class ParserUnavailable(RuntimeError):
    pass


_MATH_FONT_HINTS = ("math", "symbol", "cmmi", "cmsy", "stix", "mtmi", "euclid")
_MATH_CHARS = set("∑∫∂∇ℏαβγδεζηθικλμνξοπρστυφχψωΓΔΘΛΞΠΣΦΨΩ±×÷≈≤≥∞√")
_CODE_COMMAND_RE = re.compile(
    r"(rcsfgenerate|rncsl|rwfnestimate|rmcdhf|rci\b|jj2lsj|rtransition|"
    r"fac\.Structure|fac\b|ambit|grasp|dirac|bspline|b-spline|"
    r"R_max|rmax|kappa|k=|csf|active\s+space)",
    re.I,
)
_SECTION_NUM_RE = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+(.+)$")
_SECTION_ROMAN_RE = re.compile(r"^([IVXLC]+)(?:\.([A-Z]))?\.?\s+(.+)$")
_DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"'<>]+)", re.I)
_TABLE_CAPTION_RE = re.compile(r"^(?:Table|TABLE|Tab\.|表)\s*([IVXLC0-9.]+)", re.I)


class FormulaBackend(Protocol):
    def formula_to_latex(self, image_or_text: Any) -> str: ...


@dataclass
class SourceBlock:
    block_id: str
    page_num: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    text: str = ""
    char_start: int = 0
    char_end: int = 0
    kind: str = "text"
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class TableBlock:
    caption: str = ""
    markdown: str = ""
    rows: int = 0
    page_num: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    html: str = ""
    cells: list[list[dict[str, Any]]] = field(default_factory=list)
    header_rows: int = 1
    source_format: str = "markdown"
    block_id: str = ""


@dataclass
class FigureBlock:
    caption: str = ""
    image: str = ""
    page_num: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    block_id: str = ""


@dataclass
class Section:
    title: str
    level: int
    path: str
    paragraphs: list[str] = field(default_factory=list)
    page_start: int | None = None
    page_end: int | None = None
    tables: list[TableBlock] = field(default_factory=list)
    figures: list[FigureBlock] = field(default_factory=list)
    formulas: list[str] = field(default_factory=list)
    code_blocks: list[str] = field(default_factory=list)
    paragraph_blocks: list[SourceBlock | None] = field(default_factory=list)

    def first_paragraph(self) -> str:
        for paragraph in self.paragraphs:
            if paragraph.strip() and not paragraph.strip().startswith("|"):
                return paragraph.strip()
        return self.paragraphs[0].strip() if self.paragraphs else ""

    def text(self) -> str:
        return "\n\n".join(self.paragraphs)


@dataclass
class ParsedDocument:
    title: str = ""
    markdown: str = ""
    sections: list[Section] = field(default_factory=list)
    page_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    source_path: str = ""
    doc_id: str | None = None

    def section(self, path: str) -> Section | None:
        for section in self.sections:
            if section.path == path:
                return section
        return None

    def iter_paragraphs(self) -> Iterable[tuple[Section, int, str]]:
        for section in self.sections:
            for index, paragraph in enumerate(section.paragraphs):
                yield section, index, paragraph

    def first_page_text(self, pages: int = 2) -> str:
        if not self.source_path:
            return ""
        if fitz is None:
            return ""
        try:
            with fitz.open(self.source_path) as doc:
                return "\n".join(doc[i].get_text() for i in range(min(pages, doc.page_count)))
        except Exception:
            return ""


def _clean_cell(value: Any) -> str:
    return " ".join(str(value or "").replace("\n", " ").split()).replace("|", "\\|")


def _table_to_markdown(rows: list[list[Any]]) -> str:
    cleaned = [[_clean_cell(cell) for cell in row] for row in rows if row]
    if not cleaned:
        return ""
    width = max(len(row) for row in cleaned)
    cleaned = [row + [""] * (width - len(row)) for row in cleaned]
    header = cleaned[0]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    for row in cleaned[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _bbox_overlaps(a: tuple[float, ...], b: tuple[float, ...]) -> bool:
    ax0, ay0, ax1, ay1 = a[:4]
    bx0, by0, bx1, by1 = b[:4]
    return not (ax1 < bx0 or bx1 < ax0 or ay1 < by0 or by1 < ay0)


def _heading_level(text: str, max_size: float, body_size: float) -> int | None:
    match = _SECTION_NUM_RE.match(text)
    if match and len(text) < 180:
        return min(6, len(match.group(1).split(".")))
    match = _SECTION_ROMAN_RE.match(text)
    if match and len(text) < 180:
        return 1 if not match.group(2) else 2
    if max_size >= body_size * 1.45 and len(text) < 180:
        return 1
    if max_size >= body_size * 1.16 and len(text) < 140:
        return 2
    return None


class _MarkdownBuilder:
    def __init__(self, doc_id: str | None = None):
        self.doc_id = doc_id
        self.sections: list[Section] = []
        self.stack: list[tuple[int, str]] = []
        self.current = Section(title="Front Matter", level=0, path="Front Matter")
        self.sections.append(self.current)
        self.lines: list[str] = []

    def _start_section(self, level: int, title: str, page_num: int | None) -> None:
        while self.stack and self.stack[-1][0] >= level:
            self.stack.pop()
        self.stack.append((level, title))
        path = " > ".join(item[1] for item in self.stack)
        self.current = Section(
            title=title, level=level, path=path, page_start=page_num, page_end=page_num
        )
        self.sections.append(self.current)
        self.lines.append(f"{'#' * min(level, 6)} {title}")

    def heading(self, level: int, title: str, page_num: int | None) -> None:
        self._start_section(level, title, page_num)

    def paragraph(
        self,
        text: str,
        page_num: int | None,
        source_block: SourceBlock | None = None,
    ) -> None:
        text = text.strip()
        if not text:
            return
        self.current.paragraphs.append(text)
        self.current.paragraph_blocks.append(source_block)
        if self.current.page_start is None:
            self.current.page_start = page_num
        self.current.page_end = page_num
        self.lines.append(text)

    def table(self, table: TableBlock) -> None:
        if not table.markdown:
            return
        caption = f"{table.caption}\n" if table.caption else ""
        body = caption + table.markdown
        self.current.paragraphs.append(body)
        self.current.paragraph_blocks.append(
            SourceBlock(
                block_id=table.block_id or "",
                page_num=table.page_num,
                bbox=table.bbox,
                text=body,
                char_start=0,
                char_end=len(body),
                kind="table",
                meta={
                    "caption": table.caption,
                    "rows": table.rows,
                    "source_format": table.source_format,
                    "header_rows": table.header_rows,
                    "row_count": len(table.cells),
                    "column_count": max((len(row) for row in table.cells), default=0),
                    "cells": table.cells,
                },
            )
            if table.block_id
            else None
        )
        self.current.tables.append(table)
        self.current.page_end = table.page_num or self.current.page_end
        self.lines.append(body)

    def figure(self, figure: FigureBlock) -> None:
        if not figure.caption and not figure.image:
            return
        body = f"![{figure.caption}]({figure.image})"
        self.current.paragraphs.append(body)
        self.current.paragraph_blocks.append(
            SourceBlock(
                block_id=figure.block_id or "",
                page_num=figure.page_num,
                bbox=figure.bbox,
                text=body,
                char_start=0,
                char_end=len(body),
                kind="figure",
                meta={"caption": figure.caption, "image": figure.image},
            )
            if figure.block_id
            else None
        )
        self.current.figures.append(figure)
        self.current.page_end = figure.page_num or self.current.page_end
        self.lines.append(body)

    def formula(self, latex: str, page_num: int | None) -> None:
        body = f"$$\n{latex.strip()}\n$$"
        self.current.paragraphs.append(body)
        self.current.paragraph_blocks.append(None)
        self.current.formulas.append(latex.strip())
        self.current.page_end = page_num or self.current.page_end
        self.lines.append(body)

    def code(self, code: str, page_num: int | None) -> None:
        body = f"```\n{code.strip()}\n```"
        self.current.paragraphs.append(body)
        self.current.paragraph_blocks.append(None)
        self.current.code_blocks.append(code.strip())
        self.current.page_end = page_num or self.current.page_end
        self.lines.append(body)

    def finish(self) -> tuple[list[Section], str]:
        sections = [section for section in self.sections if section.paragraphs or section.level == 0]
        return sections, "\n\n".join(self.lines).strip()


def _mineru_heading_level(text: str) -> int:
    match = re.match(r"^(\d+(?:\.\d+)*)[\.\s]", str(text or "").strip())
    if not match:
        return 2
    return max(2, min(6, match.group(1).count(".") + 2))


def _pdf_bbox(raw: Any, width: float, height: float) -> tuple[float, float, float, float] | None:
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    try:
        values = [float(item) for item in raw]
    except (TypeError, ValueError):
        return None
    if max(values) <= 1.5:
        values = [
            values[0] * width,
            values[1] * height,
            values[2] * width,
            values[3] * height,
        ]
    return max(0.0, values[0]), max(0.0, values[1]), max(0.0, values[2]), max(0.0, values[3])


class PDFParser:
    """Local PyMuPDF parser with optional external formula backend."""

    def __init__(
        self,
        *,
        formula_backend: FormulaBackend | None = None,
        skip_page_numbers: bool = True,
        allow_network: bool = True,
        ocr_callback: Callable[[bytes, str], str] | None = None,
        mineru_backend: MinerUConverter | None = None,
        mineru_fallback: bool = True,
    ):
        self.formula_backend = formula_backend
        self.skip_page_numbers = skip_page_numbers
        self.allow_network = allow_network
        self.ocr_callback = ocr_callback
        self.mineru_backend = mineru_backend
        self.mineru_fallback = mineru_fallback
        self.last_mineru_error = ""

    def parse(
        self,
        pdf_path: str | Path,
        *,
        doc_id: str | None = None,
        title: str | None = None,
    ) -> ParsedDocument:
        path = Path(pdf_path)
        if fitz is None:
            raise ParserUnavailable("PyMuPDF (fitz) 未安装")
        if not path.exists():
            raise FileNotFoundError(path)
        if self.mineru_backend is not None and self.mineru_backend.enabled:
            try:
                result = self.mineru_backend.convert(
                    path, doc_id=doc_id or path.stem
                )
                return self._finish_mineru_parse(
                    path,
                    result,
                    doc_id=doc_id,
                    title=title,
                )
            except Exception as exc:
                self.last_mineru_error = str(exc)
                log.warning("MinerU parse failed for %s: %s", path, exc)
                if not self.mineru_fallback:
                    if isinstance(exc, MinerUError):
                        raise
                    raise ParserUnavailable(f"MinerU 解析失败：{exc}") from exc
        with fitz.open(path) as doc:
            page_dicts = [doc[i].get_text("dict") for i in range(doc.page_count)]
            sizes: list[float] = []
            for page in page_dicts:
                for block in page.get("blocks", []):
                    for line in block.get("lines", []):
                        for span in line.get("spans", []):
                            if span.get("text", "").strip():
                                sizes.append(float(span.get("size", 10)))
            body_size = statistics.median(sizes) if sizes else 10.0
            builder = _MarkdownBuilder(doc_id=doc_id)
            page_count = doc.page_count
            metadata = dict(doc.metadata or {})
            block_prefix = doc_id or path.stem

            for page_index in range(page_count):
                page = doc[page_index]
                page_num = page_index + 1
                tables: list[TableBlock] = []
                table_rects: list[tuple[float, float, float, float]] = []
                try:
                    found = page.find_tables()
                    for table in found.tables:
                        rows = table.extract()
                        markdown = _table_to_markdown(rows)
                        if not markdown:
                            continue
                        bbox = tuple(float(v) for v in table.bbox)
                        caption = self._find_caption(page_dicts[page_index], bbox, body_size)
                        row_count = max(1, int(getattr(table, "row_count", len(rows)) or len(rows)))
                        col_count = max(1, int(getattr(table, "col_count", 0) or 0))
                        if not col_count:
                            col_count = max((len(row) for row in rows), default=1)
                        header_rows = max(0, int(getattr(table, "header_rows", 0) or 0))
                        cells = [
                            [
                                {
                                    "row": row_index,
                                    "col": col_index,
                                    "text": str(rows[row_index][col_index] or "")
                                    if row_index < len(rows) and col_index < len(rows[row_index])
                                    else "",
                                    "header": row_index < header_rows,
                                    "rowspan": 1,
                                    "colspan": 1,
                                    "bbox": None,
                                }
                                for col_index in range(col_count)
                            ]
                            for row_index in range(row_count)
                        ]
                        for cell_index, cell_bbox in enumerate(getattr(table, "cells", []) or []):
                            row_index = cell_index % row_count
                            col_index = cell_index // row_count
                            if 0 <= row_index < row_count and 0 <= col_index < col_count:
                                try:
                                    cells[row_index][col_index]["bbox"] = [
                                        float(value) for value in cell_bbox
                                    ]
                                except (TypeError, ValueError):
                                    pass
                        tables.append(
                            TableBlock(
                                caption=caption,
                                markdown=markdown,
                                rows=max(0, len(rows) - 1),
                                page_num=page_num,
                                bbox=bbox,
                                cells=cells,
                                header_rows=header_rows,
                                source_format="pymupdf",
                            )
                        )
                        table_rects.append(bbox)
                except Exception as exc:
                    log.debug("Table extraction skipped on page %s: %s", page_num, exc)

                blocks = []
                for block in page_dicts[page_index].get("blocks", []):
                    if block.get("type") != 0:
                        continue
                    spans = [
                        span
                        for line in block.get("lines", [])
                        for span in line.get("spans", [])
                        if span.get("text", "").strip()
                    ]
                    if not spans:
                        continue
                    text = " ".join(span.get("text", "").strip() for span in spans).strip()
                    if not text:
                        continue
                    bbox = tuple(float(v) for v in block.get("bbox", (0, 0, 0, 0)))
                    if any(_bbox_overlaps(bbox, rect) for rect in table_rects):
                        continue
                    if self._is_page_number(text, spans, block, page.rect.height, body_size):
                        continue
                    max_size = max(float(span.get("size", body_size)) for span in spans)
                    fonts = " ".join(str(span.get("font", "")) for span in spans).lower()
                    blocks.append((bbox[1], text, max_size, fonts, bbox))

                items: list[tuple[float, str, Any]] = [
                    (y, "text", (text, max_size, fonts, bbox))
                    for y, text, max_size, fonts, bbox in blocks
                ]
                items.extend(
                    (
                        table.bbox[1] if table.bbox else 0.0,
                        "table",
                        table,
                    )
                    for table in tables
                )
                items.sort(key=lambda item: item[0])
                for block_index, (_y, kind, payload) in enumerate(items):
                    block_id = f"{block_prefix}:p{page_num}:b{block_index:03d}"
                    if kind == "table":
                        payload.block_id = block_id
                        builder.table(payload)
                        continue
                    text, max_size, fonts, bbox = payload
                    level = _heading_level(text, max_size, body_size)
                    if level is not None and not self._looks_like_formula(text, fonts):
                        builder.heading(level, text, page_num)
                        continue
                    if self._looks_like_formula(text, fonts):
                        latex = self._formula(text)
                        builder.formula(latex, page_num)
                        continue
                    if self._looks_like_code(text, fonts):
                        builder.code(text, page_num)
                        continue
                    builder.paragraph(
                        text,
                        page_num,
                        source_block=SourceBlock(
                            block_id=block_id,
                            page_num=page_num,
                            bbox=bbox,
                            text=text,
                            char_start=0,
                            char_end=len(text),
                            kind="text",
                            meta={"font_size": max_size, "fonts": fonts},
                        ),
                    )

            sections, markdown = builder.finish()
            extracted: DocumentMetadata = extract_document_metadata(
                path,
                doc=doc,
                allow_network=self.allow_network,
                ocr_callback=self.ocr_callback,
            )
            first_content = next(
                (
                    section
                    for section in sections
                    if section.title != "Front Matter" or section.paragraphs
                ),
                None,
            )
            metadata_title = str(metadata.get("title") or "").strip()
            section_title = first_content.title if first_content else ""
            final_title = (
                (title if is_valid_title(title) else "")
                or (extracted.title if is_valid_title(extracted.title) else "")
                or (metadata_title if is_valid_title(metadata_title) else "")
                or (section_title if is_valid_title(section_title) else "")
                or path.stem
            )
            merged_metadata = dict(metadata)
            merged_metadata.update(extracted.as_dict())
            merged_metadata["markdown_backend"] = "pymupdf"
            if self.last_mineru_error:
                merged_metadata["mineru_fallback_error"] = self.last_mineru_error
            return ParsedDocument(
                title=final_title,
                markdown=markdown,
                sections=sections,
                page_count=page_count,
                metadata=merged_metadata,
                source_path=str(path),
                doc_id=doc_id,
            )

    def parse_mineru_blocks(
        self,
        blocks: list[dict[str, Any]],
        page_sizes: list[tuple[float, float]],
        *,
        title: str = "",
        doc_id: str | None = None,
        page_count: int = 0,
        metadata: dict[str, Any] | None = None,
        source_path: str = "",
    ) -> ParsedDocument:
        """Build ParsedDocument from MinerU blocks while keeping page/bbox IDs."""
        builder = _MarkdownBuilder(doc_id=doc_id)
        skip_kinds = {"header", "footer", "page_header", "page_footer", "page_number"}
        for block in blocks:
            if not isinstance(block, Mapping):
                continue
            kind = str(block.get("type") or "").lower()
            if kind in skip_kinds:
                continue
            try:
                page_num = int(block.get("page_num") or 1)
            except (TypeError, ValueError):
                page_num = 1
            width, height = (
                page_sizes[page_num - 1]
                if 0 < page_num <= len(page_sizes)
                else (1.0, 1.0)
            )
            bbox = _pdf_bbox(block.get("bbox"), width, height)
            block_id = str(block.get("block_id") or "")
            text = str(block.get("text") or "")
            caption = str(block.get("caption") or "")
            markdown = str(block.get("markdown") or text)
            if kind in ("doc_title", "title"):
                if text:
                    builder.heading(1, text, page_num)
                continue
            if kind in ("paragraph_title", "section_title", "heading"):
                if text:
                    builder.heading(_mineru_heading_level(text), text, page_num)
                continue
            if kind == "table":
                if not markdown.strip():
                    continue
                cells = block.get("cells") or []
                is_html_table = "<table" in text.lower()
                header_rows = block.get("header_rows")
                builder.table(
                    TableBlock(
                        caption=caption,
                        markdown=markdown,
                        rows=max(0, len(markdown.splitlines()) - 2),
                        page_num=page_num,
                        bbox=bbox,
                        html=text if is_html_table else "",
                        cells=cells,
                        header_rows=1 if header_rows is None else int(header_rows),
                        source_format="html" if is_html_table else "markdown",
                        block_id=block_id,
                    )
                )
                continue
            if kind in ("equation", "formula"):
                latex = text.strip()
                if latex.startswith("$$") and latex.endswith("$$"):
                    latex = latex[2:-2].strip()
                if latex:
                    builder.formula(latex, page_num)
                continue
            if kind in ("image", "figure", "chart"):
                image = str(block.get("image_source") or "")
                if image or caption:
                    builder.figure(
                        FigureBlock(
                            caption=caption,
                            image=image,
                            page_num=page_num,
                            bbox=bbox,
                            block_id=block_id,
                        )
                    )
                continue
            if not text:
                continue
            builder.paragraph(
                text,
                page_num,
                source_block=SourceBlock(
                    block_id=block_id,
                    page_num=page_num,
                    bbox=bbox,
                    text=text,
                    char_start=0,
                    char_end=len(text),
                    kind=kind or "text",
                    meta={"mineru_type": kind},
                ),
            )
        sections, markdown = builder.finish()
        first_content = next(
            (
                section
                for section in sections
                if section.title != "Front Matter" or section.paragraphs
            ),
            None,
        )
        return ParsedDocument(
            title=title or (first_content.title if first_content else "") or "Untitled",
            markdown=markdown,
            sections=sections,
            page_count=page_count,
            metadata=dict(metadata or {}),
            source_path=source_path,
            doc_id=doc_id,
        )

    def _finish_mineru_parse(
        self,
        path: Path,
        result: Any,
        *,
        doc_id: str | None,
        title: str | None,
    ) -> ParsedDocument:
        pdf_metadata: dict[str, Any] = {}
        extracted: DocumentMetadata | None = None
        page_sizes: list[tuple[float, float]] = []
        page_count = int(getattr(result, "page_count", 0) or 0)
        try:
            with fitz.open(path) as doc:  # type: ignore[union-attr]
                pdf_metadata = dict(doc.metadata or {})
                page_count = page_count or int(doc.page_count)
                page_sizes = [
                    (float(page.rect.width), float(page.rect.height))
                    for page in doc
                ]
                extracted = extract_document_metadata(
                    path,
                    doc=doc,
                    allow_network=self.allow_network,
                    ocr_callback=self.ocr_callback,
                )
        except Exception:
            log.debug("MinerU metadata extraction failed", exc_info=True)
        metadata: dict[str, Any] = dict(pdf_metadata)
        mineru_meta = {
            "markdown_backend": "mineru",
            "mineru_version": str(getattr(result, "version", "") or ""),
            "mineru_tier": str(getattr(result, "tier", "") or ""),
            "mineru_elapsed_sec": round(
                float(getattr(result, "elapsed_sec", 0.0) or 0.0), 3
            ),
            "mineru_output_dir": str(getattr(result, "output_dir", "") or ""),
        }
        metadata.update(mineru_meta)
        if extracted is not None:
            metadata.update(extracted.as_dict())
        blocks = getattr(result, "blocks", None) or []
        if blocks:
            parsed = self.parse_mineru_blocks(
                blocks,
                page_sizes,
                title="",
                doc_id=doc_id,
                page_count=page_count,
                metadata=metadata,
                source_path=str(path),
            )
        else:
            parsed = self.parse_markdown(
                result.markdown,
                title="",
                doc_id=doc_id,
                page_count=page_count,
                metadata=metadata,
                source_path=str(path),
                page_markdown=result.page_markdown,
            )
        first_content = next(
            (
                section
                for section in parsed.sections
                if section.title != "Front Matter" or section.paragraphs
            ),
            None,
        )
        title_candidates = (
            title,
            extracted.title if extracted is not None else "",
            str(pdf_metadata.get("title") or ""),
            first_content.title if first_content else "",
            path.stem,
        )
        parsed.title = next(
            (
                candidate
                for candidate in title_candidates
                if candidate and is_valid_title(str(candidate))
            ),
            path.stem,
        )
        return parsed

    @staticmethod
    def _is_page_number(text: str, spans: list[Any], block: dict, page_height: float, body_size: float) -> bool:
        stripped = text.strip()
        if re.fullmatch(r"[-–—]?\s*\d{1,4}\s*[-–—]?", stripped):
            return True
        max_size = max(float(span.get("size", body_size)) for span in spans)
        y0 = float(block.get("bbox", (0, 0, 0, 0))[1])
        y1 = float(block.get("bbox", (0, 0, 0, 0))[3])
        if max_size < body_size * 0.7 and (y1 < page_height * 0.07 or y0 > page_height * 0.93):
            return True
        return False

    @staticmethod
    def _looks_like_formula(text: str, fonts: str) -> bool:
        if "$" in text and "=" in text:
            return True
        if any(hint in fonts for hint in _MATH_FONT_HINTS) and "=" in text:
            return True
        if "=" in text and any(char in text for char in _MATH_CHARS):
            return True
        if (
            len(text) < 150
            and text.count(" ") <= 12
            and re.match(r"^[A-Za-zα-ωΑ-Ω\\{\(\[][^.!?]{0,120}=\s*", text)
        ):
            return True
        return False

    def _formula(self, text: str) -> str:
        if self.formula_backend is not None:
            try:
                return self.formula_backend.formula_to_latex(text)
            except Exception:
                log.debug("Formula backend failed; keeping extracted text", exc_info=True)
        return text.strip()

    @staticmethod
    def _looks_like_code(text: str, fonts: str) -> bool:
        if "courier" in fonts or "mono" in fonts:
            return True
        return bool(_CODE_COMMAND_RE.search(text)) and len(text) < 600

    @staticmethod
    def _find_caption(page: dict, table_bbox: tuple[float, ...], body_size: float) -> str:
        candidates: list[tuple[float, str]] = []
        for block in page.get("blocks", []):
            if block.get("type") != 0:
                continue
            spans = [
                span
                for line in block.get("lines", [])
                for span in line.get("spans", [])
                if span.get("text", "").strip()
            ]
            text = " ".join(span.get("text", "").strip() for span in spans).strip()
            if not text or not _TABLE_CAPTION_RE.match(text):
                continue
            bbox = tuple(float(v) for v in block.get("bbox", (0, 0, 0, 0)))
            distance = abs(bbox[3] - table_bbox[1])
            if bbox[3] <= table_bbox[1] + 8 and distance < 80:
                candidates.append((distance, text))
        candidates.sort(key=lambda item: item[0])
        return candidates[0][1] if candidates else ""

    # ------------------------------------------------------------- markdown
    def parse_markdown(
        self,
        markdown: str,
        *,
        title: str = "",
        doc_id: str | None = None,
        page_count: int = 0,
        metadata: dict[str, Any] | None = None,
        source_path: str = "",
        page_markdown: list[tuple[int, str]] | None = None,
    ) -> ParsedDocument:
        builder = _MarkdownBuilder(doc_id=doc_id)
        buffer: list[str] = []
        in_code = False
        in_formula = False
        in_table = False
        in_html_table = False
        html_buffer: list[str] = []
        html_table_seen = False
        current_page: int | None = None

        if page_markdown:
            markdown = "\n\n".join(
                f"<!-- ACDB-PAGE: {page} -->\n{text}"
                for page, text in page_markdown
            )

        def flush() -> None:
            if not buffer:
                return
            text = "\n".join(buffer).strip()
            buffer.clear()
            if not text:
                return
            if text.startswith("```"):
                builder.code(text.strip("`\n "), current_page)
            elif text.startswith("$$"):
                builder.formula(text.strip("$\n "), current_page)
            elif text.lstrip().startswith("|"):
                rows = [line for line in text.splitlines() if line.strip().startswith("|")]
                caption = ""
                if builder.current.paragraphs:
                    candidate = builder.current.paragraphs[-1].strip()
                    if _TABLE_CAPTION_RE.match(candidate):
                        caption = builder.current.paragraphs.pop()
                        if builder.current.paragraph_blocks:
                            builder.current.paragraph_blocks.pop()
                        if builder.lines and builder.lines[-1] == candidate:
                            builder.lines.pop()
                table_cells = markdown_table_cells(text)
                builder.table(
                    TableBlock(
                        caption=caption,
                        markdown=text,
                        rows=max(0, len(rows) - 2),
                        page_num=current_page,
                        source_format="markdown",
                        cells=table_cells,
                        header_rows=table_header_rows(table_cells),
                        block_id=f"md:p{current_page or 0}:t{len(builder.current.tables)}",
                    )
                )
            else:
                builder.paragraph(text, current_page)

        def emit_html_table(html_text: str) -> None:
            nonlocal html_table_seen
            html_table_seen = True
            flush()
            table_markdown = html_table_to_markdown(html_text)
            if not table_markdown:
                builder.paragraph(html_text, current_page)
                return
            caption = ""
            if builder.current.paragraphs:
                candidate = builder.current.paragraphs[-1].strip()
                if _TABLE_CAPTION_RE.match(candidate):
                    caption = builder.current.paragraphs.pop()
                    if builder.current.paragraph_blocks:
                        builder.current.paragraph_blocks.pop()
                    if builder.lines and builder.lines[-1] == candidate:
                        builder.lines.pop()
            table_cells = html_table_cells(html_text)
            builder.table(
                TableBlock(
                    caption=caption,
                    markdown=table_markdown,
                    rows=max(0, len(table_markdown.splitlines()) - 2),
                    page_num=current_page,
                    html=html_text,
                    cells=table_cells,
                    header_rows=table_header_rows(table_cells),
                    source_format="html",
                    block_id=f"md:p{current_page or 0}:t{len(builder.current.tables)}",
                )
            )

        for raw_line in markdown.splitlines():
            line = raw_line.rstrip()
            if in_html_table:
                html_buffer.append(line)
                if "</table>" in line.lower():
                    html_text = "\n".join(html_buffer)
                    html_buffer.clear()
                    in_html_table = False
                    emit_html_table(html_text)
                continue
            page_match = re.fullmatch(
                r"<!--\s*ACDB-PAGE:\s*(\d+)\s*-->", line.strip()
            )
            if page_match:
                flush()
                current_page = int(page_match.group(1))
                continue
            if in_code:
                buffer.append(line)
                if line.strip().startswith("```"):
                    in_code = False
                    flush()
                continue
            if in_formula:
                buffer.append(line)
                if "$$" in line:
                    in_formula = False
                    flush()
                continue
            heading = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
            if heading:
                flush()
                builder.heading(
                    len(heading.group(1)), heading.group(2).strip(), current_page
                )
                continue
            if line.strip().startswith("```"):
                flush()
                in_code = True
                buffer.append(line)
                continue
            if line.strip().startswith("$$"):
                flush()
                if line.strip().count("$$") >= 2:
                    builder.formula(
                        line.strip().strip("$").strip(), current_page
                    )
                else:
                    in_formula = True
                    buffer.append(line)
                continue
            image_match = re.fullmatch(r"!\[([^\]]*)\]\(([^)]+)\)\s*", line.strip())
            if image_match:
                flush()
                builder.figure(
                    FigureBlock(
                        caption=image_match.group(1).strip(),
                        image=image_match.group(2).strip(),
                        page_num=current_page,
                        block_id=f"md:p{current_page or 0}:f{len(builder.current.figures)}",
                    )
                )
                continue
            if line.lstrip().lower().startswith("<table"):
                flush()
                html_buffer = [line]
                if "</table>" in line.lower():
                    emit_html_table(line)
                else:
                    in_html_table = True
                continue
            if line.strip().startswith("|"):
                if not in_table:
                    flush()
                    in_table = True
                buffer.append(line)
                continue
            if in_table:
                in_table = False
                flush()
            if not line.strip():
                flush()
                continue
            buffer.append(line)
        if in_html_table and html_buffer:
            emit_html_table("\n".join(html_buffer))
        flush()

        sections, _ = builder.finish()
        if html_table_seen:
            markdown = re.sub(
                r"<table[^>]*>.*?</table>",
                lambda match: html_table_to_markdown(match.group(0)),
                markdown,
                flags=re.IGNORECASE | re.DOTALL,
            )
        first_content = next(
            (
                section
                for section in sections
                if section.title != "Front Matter" or section.paragraphs
            ),
            None,
        )
        final_title = title or (first_content.title if first_content else "")
        return ParsedDocument(
            title=final_title or "Untitled",
            markdown=markdown,
            sections=sections,
            page_count=page_count,
            metadata=dict(metadata or {}),
            source_path=source_path,
            doc_id=doc_id,
        )


def latex_source_to_markdown(source_path: str | Path) -> str:
    """Best-effort conversion of an arXiv LaTeX source archive to Markdown.

    This keeps section headings, display equations and text while discarding
    the preamble.  It is intentionally lossy; for complex macros the PDF path
    remains the primary source of truth.
    """
    import tarfile
    import zipfile

    path = Path(source_path)
    sources: list[tuple[str, bytes]] = []
    if path.suffix.lower() in (".gz", ".tgz", ".tar"):
        with tarfile.open(path, "r:*") as archive:
            for member in archive.getmembers():
                if not member.isfile():
                    continue
                pure = PurePosixPath(member.name.replace("\\", "/"))
                if pure.is_absolute() or ".." in pure.parts:
                    continue
                if member.name.lower().endswith((".tex", ".bbl")):
                    handle = archive.extractfile(member)
                    if handle is not None:
                        sources.append((member.name, handle.read()))
    elif path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                pure = PurePosixPath(name)
                if pure.is_absolute() or ".." in pure.parts:
                    continue
                if name.lower().endswith(".tex"):
                    sources.append((name, archive.read(name)))
    elif path.suffix.lower() == ".tex":
        sources.append((path.name, path.read_bytes()))
    else:
        raise ParserUnavailable(f"不支持的 LaTeX 源格式: {path.suffix}")

    if not sources:
        raise ParserUnavailable("LaTeX 源包中没有 .tex 文件")

    main_name, main_bytes = max(
        sources, key=lambda item: b"\\documentclass" in item[1]
    )
    try:
        text = main_bytes.decode("utf-8")
    except UnicodeDecodeError:
        text = main_bytes.decode("latin-1", errors="replace")
    try:
        other_text = "\n".join(
            item[1].decode("utf-8", errors="replace")
            for item in sources
            if item[0] != main_name and item[0].lower().endswith(".tex")
        )
    except Exception:
        other_text = ""
    if "\\begin{document}" in text:
        text = text.split("\\begin{document}", 1)[1]
    if "\\end{document}" in text:
        text = text.split("\\end{document}", 1)[0]
    text = text.replace("\\%", "％").replace("\\&", "&")
    text = re.sub(r"(?<!\\)%.*$", "", text, flags=re.M)
    text = re.sub(
        r"\\(?:section|chapter)\*?\{([^{}]+)\}", r"\n## \1\n", text
    )
    text = re.sub(r"\\subsection\*?\{([^{}]+)\}", r"\n### \1\n", text)
    text = re.sub(r"\\subsubsection\*?\{([^{}]+)\}", r"\n#### \1\n", text)
    text = re.sub(
        r"\\begin\{(?:equation\*?|align\*?|eqnarray\*?|displaymath)\}(.*?)"
        r"\\end\{(?:equation\*?|align\*?|eqnarray\*?|displaymath)\}",
        lambda match: "\n$$\n" + match.group(1).strip() + "\n$$\n",
        text,
        flags=re.S,
    )
    text = re.sub(r"\\\[(.*?)\\\]", lambda match: "\n$$\n" + match.group(1).strip() + "\n$$\n", text, flags=re.S)
    text = re.sub(r"\\[a-zA-Z]+\*?(?:\[[^\]]*\])?", " ", text)
    text = text.replace("{", "").replace("}", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    markdown = text.strip()
    if other_text and len(markdown) < 500:
        markdown += "\n\n" + other_text
    return markdown


def extract_pdf_identity(pdf_path: str | Path, pages: int = 2) -> dict[str, Any]:
    """Extract DOI / title / journal / year for manual-drop matching."""
    path = Path(pdf_path)
    result: dict[str, Any] = {
        "doi": None,
        "title": "",
        "journal": "",
        "year": None,
        "authors": [],
        "text": "",
        "page_count": None,
    }
    if fitz is None or not path.exists():
        return result
    try:
        with fitz.open(path) as doc:
            result["page_count"] = doc.page_count
            text = "\n".join(
                doc[index].get_text() for index in range(min(pages, doc.page_count))
            )
            result["text"] = text
            identity = extract_document_metadata(path, doc=doc, allow_network=False)
            if is_valid_title(identity.title):
                result["title"] = identity.title
            result["journal"] = identity.journal or ""
            result["year"] = identity.year
            result["authors"] = list(identity.authors or [])
            if identity.doi:
                result["doi"] = identity.doi
            if not result["doi"]:
                match = _DOI_RE.search(text)
                if match:
                    result["doi"] = match.group(1).rstrip(".,;)]}")
            if not result["title"]:
                first_page = doc[0].get_text("dict") if doc.page_count else {}
                candidates: list[tuple[float, str]] = []
                for block in first_page.get("blocks", []):
                    spans = [
                        span
                        for line in block.get("lines", [])
                        for span in line.get("spans", [])
                        if span.get("text", "").strip()
                    ]
                    if not spans:
                        continue
                    text_block = " ".join(span.get("text", "").strip() for span in spans).strip()
                    size = max(float(span.get("size", 0)) for span in spans)
                    if 4 < len(text_block) < 250 and is_valid_title(text_block):
                        candidates.append((size, text_block))
                candidates.sort(reverse=True)
                if candidates:
                    result["title"] = candidates[0][1]
    except Exception as exc:
        log.debug("PDF identity extraction failed for %s: %s", path, exc)
    return result
