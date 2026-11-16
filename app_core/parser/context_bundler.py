"""Three-point context stitching.

The central rule: never send an isolated conclusion sentence to the LLM.  Each
:class:`ContextBundle` contains

1. the first paragraph of the current section (global premise),
2. back-traced definitions for ``Model B`` / ``Set II`` / ``Table I`` / ``Eq. (3)``
   references,
3. the focal paragraph with one paragraph of context on each side, with the
   target sentence wrapped in ``⟪FOCUS⟫ ... ⟪/FOCUS⟫``.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from pydantic import BaseModel, Field

from parser.conclusion_tagger import tag_text
from parser.four_channel_radar import RadarHit, RadarResult
from parser.pdf_to_markdown import ParsedDocument, Section
from parser.preamble_extractor import extract_document_preamble
from parser.table_compressor import compress_markdown_table

_MODEL_REF_RE = re.compile(
    r"\b(Model|Set|Scheme|Case|Approach|Method)\s+([A-Z][A-Za-z0-9-]*)\b"
)
_TABLE_REF_RE = re.compile(r"\b(Table|TABLE|表)\s*([IVXLC0-9]+)\b")
_EQ_REF_RE = re.compile(r"\b(?:Eq|Equation|式)\.?\s*\(?([0-9]+)\)?", re.I)
_SECTION_REF_RE = re.compile(r"\b(?:Sec|Section|章节?)\.?\s*([IVXLC0-9.]+)", re.I)
_DEFINITION_CUE_RE = re.compile(
    r"(is defined|refers? to|denotes?|consists? of|corresponds? to|"
    r"includes?|comprises?|采用|定义为|指|包括|即|如表)",
    re.I,
)


class ContextBundle(BaseModel):
    doc_id: str
    section_path: str
    global_premise_paragraph: str = ""
    backtraced_definitions: list[str] = Field(default_factory=list)
    document_preamble: list[str] = Field(default_factory=list)
    focal_window_markdown: str = ""
    detected_channels: list[str] = Field(default_factory=list)
    preliminary_tags_bitmask: int = 0
    focal_sentence: str = ""
    page_num: int | None = None
    table_markdown: str = ""
    formula_latex: str = ""
    code_snippet: str = ""
    locator: dict[str, Any] = Field(default_factory=dict)

    def prompt_markdown(self) -> str:
        definitions = "\n\n".join(
            f"[回溯定义 {index + 1}] {item}"
            for index, item in enumerate(self.backtraced_definitions)
        ) or "（无显式代号引用）"
        parts = [
            f"# ContextBundle · {self.doc_id}",
            f"## 小节\n{self.section_path}",
        ]
        if self.document_preamble:
            preamble_text = "\n".join(f"- {p}" for p in self.document_preamble)
            parts.append(f"## 全局公理与一次性声明 (Document Preamble)\n{preamble_text}")
        parts.extend([
            f"## 全局前提（小节首段）\n{self.global_premise_paragraph}",
            f"## 回溯定义\n{definitions}",
            f"## 焦点窗口\n{self.focal_window_markdown}",
            f"## 检测通道\n{', '.join(self.detected_channels)}",
            f"## 预打标位掩码\n{self.preliminary_tags_bitmask}",
        ])
        if self.table_markdown:
            parts.append(f"## 表格数据 (压缩脱水版)\n{self.table_markdown}")
        return "\n".join(parts)


class ContextBundler:
    def build(
        self,
        document: ParsedDocument,
        radar: RadarResult,
        *,
        doc_id: str | None = None,
        max_bundles: int = 200,
        manual: bool = False,
    ) -> list[ContextBundle]:
        if manual and max_bundles == 200:
            max_bundles = 2000
        preamble = extract_document_preamble(document)
        grouped: dict[tuple[str, int], list[RadarHit]] = {}
        for hit in radar.hits:
            key = (hit.section_path, hit.paragraph_index)
            grouped.setdefault(key, []).append(hit)

        bundles: list[ContextBundle] = []
        for (section_path, paragraph_index), hits in grouped.items():
            section = document.section(section_path)
            if section is None:
                section = next((item for item in document.sections if item.paragraphs), None)
            if section is None:
                continue
            if paragraph_index >= len(section.paragraphs):
                paragraph_index = 0
            hit = hits[0]
            paragraph = section.paragraphs[paragraph_index] if section.paragraphs else ""
            previous = section.paragraphs[paragraph_index - 1] if paragraph_index > 0 else ""
            following = (
                section.paragraphs[paragraph_index + 1]
                if paragraph_index + 1 < len(section.paragraphs)
                else ""
            )
            focal_sentence = hit.sentence or self._first_sentence(paragraph)
            focal = self._highlight(paragraph, focal_sentence, hit)
            window = "\n\n".join(part for part in (previous, focal, following) if part)
            paragraph_blocks = getattr(section, "paragraph_blocks", [])
            source_block = (
                paragraph_blocks[paragraph_index]
                if paragraph_index < len(paragraph_blocks)
                else None
            )
            char_start = paragraph.find(focal_sentence)
            if char_start < 0:
                char_start = 0
            char_end = char_start + len(focal_sentence)
            tags = tag_text(focal_sentence or paragraph)
            bitmask = tags.bitmask
            for item in hits:
                bitmask |= item.tags_bitmask
            channels = sorted({item.channel for item in hits})
            definitions = self._backtrace(document, section, paragraph_index, paragraph)
            bundles.append(
                ContextBundle(
                    doc_id=doc_id or document.doc_id or "",
                    section_path=section.path,
                    global_premise_paragraph=self._global_premise(section),
                    backtraced_definitions=definitions,
                    document_preamble=preamble,
                    focal_window_markdown=window,
                    detected_channels=channels,
                    preliminary_tags_bitmask=bitmask,
                    focal_sentence=focal_sentence,
                    page_num=hit.page_num or section.page_start,
                    table_markdown=compress_markdown_table(hit.table_markdown) if hit.table_markdown else "",
                    formula_latex=hit.meta.get("formula", "") if hit.channel == "FORMULA" else "",
                    code_snippet=hit.meta.get("code", "") if hit.channel == "CODE" else "",
                    locator={
                        "source": "radar_bundle",
                        "page_num": hit.page_num or section.page_start,
                        "section_path": section.path,
                        "paragraph_index": paragraph_index,
                        "channels": channels,
                        "focal_sentence": focal_sentence,
                        "table": bool(hit.table_markdown),
                        "formula": hit.channel == "FORMULA",
                        "code": hit.channel == "CODE",
                        "figure": hit.channel == "FIGURE",
                        "figure_image": str(hit.meta.get("figure") or ""),
                        "markdown_backend": str(
                            (document.metadata or {}).get("markdown_backend") or ""
                        ),
                        "block_id": getattr(source_block, "block_id", "") or "",
                        "bbox": list(source_block.bbox)
                        if source_block is not None and source_block.bbox
                        else None,
                        "block_kind": getattr(source_block, "kind", "") or "",
                        "block_char_start": getattr(source_block, "char_start", 0) or 0,
                        "block_char_end": getattr(source_block, "char_end", 0) or 0,
                        "char_start": char_start,
                        "char_end": char_end,
                        "table_caption": str(
                            ((getattr(source_block, "meta", {}) or {}).get("caption") or "")
                        ),
                        "table_source_format": str(
                            ((getattr(source_block, "meta", {}) or {}).get("source_format") or "")
                        ),
                        "table_header_rows": int(
                            ((getattr(source_block, "meta", {}) or {}).get("header_rows") or 0)
                        ),
                        "table_row_count": int(
                            ((getattr(source_block, "meta", {}) or {}).get("row_count") or 0)
                        ),
                        "table_column_count": int(
                            ((getattr(source_block, "meta", {}) or {}).get("column_count") or 0)
                        ),
                        "table_cells": (
                            (getattr(source_block, "meta", {}) or {}).get("cells") or []
                        )[:500],
                        "block_figure_image": str(
                            ((getattr(source_block, "meta", {}) or {}).get("image") or "")
                        ),
                    },
                )
            )
            if len(bundles) >= max_bundles:
                break
        return bundles

    # ------------------------------------------------------------------ text
    @staticmethod
    def _first_sentence(text: str) -> str:
        match = re.split(r"(?<=[.!?。！？])\s+|\n+", text.strip(), maxsplit=1)
        return match[0].strip() if match else text.strip()

    @staticmethod
    def _highlight(paragraph: str, sentence: str, hit: RadarHit) -> str:
        if not paragraph:
            return ""
        if hit.channel in ("STRATEGY_TABLE", "BULK_DATA_TABLE", "DATA_TABLE"):
            return f"⟪FOCUS⟫ {paragraph.strip()} ⟪/FOCUS⟫"
        if hit.channel in ("FORMULA", "CODE") and not sentence:
            return f"⟪FOCUS⟫ {paragraph.strip()} ⟪/FOCUS⟫"
        if sentence and sentence in paragraph:
            return paragraph.replace(
                sentence, f"⟪FOCUS⟫ {sentence} ⟪/FOCUS⟫", 1
            )
        return f"⟪FOCUS⟫ {paragraph.strip()} ⟪/FOCUS⟫"

    @staticmethod
    def _global_premise(section: Section) -> str:
        for paragraph in section.paragraphs:
            if not paragraph.lstrip().startswith(("|", "```", "$$")):
                return paragraph.strip()
        return section.paragraphs[0].strip() if section.paragraphs else ""

    # -------------------------------------------------------------- backtrace
    def _backtrace(
        self,
        document: ParsedDocument,
        section: Section,
        paragraph_index: int,
        focal_text: str,
    ) -> list[str]:
        references = self._references(focal_text)
        if not references:
            return []
        definitions: list[str] = []
        seen: set[str] = set()
        paragraphs = list(document.iter_paragraphs())
        focal_position = next(
            (
                position
                for position, (sec, index, _text) in enumerate(paragraphs)
                if sec.path == section.path and index == paragraph_index
            ),
            len(paragraphs),
        )
        for kind, raw, number in references:
            if kind == "table":
                table_text = self._find_table(document, number)
                if table_text and table_text not in seen:
                    seen.add(table_text)
                    definitions.append(table_text)
                continue
            label = f"{raw} {number}"
            for position, (sec, index, text) in enumerate(paragraphs):
                if position >= focal_position:
                    break
                if label.lower() not in text.lower():
                    continue
                if _DEFINITION_CUE_RE.search(text):
                    if text not in seen:
                        seen.add(text)
                        definitions.append(text)
                    break
        return definitions[:8]

    @staticmethod
    def _references(text: str) -> list[tuple[str, str, str]]:
        refs: list[tuple[str, str, str]] = []
        for match in _MODEL_REF_RE.finditer(text):
            refs.append(("model", match.group(1), match.group(2)))
        for match in _TABLE_REF_RE.finditer(text):
            refs.append(("table", match.group(1), match.group(2)))
        for match in _EQ_REF_RE.finditer(text):
            refs.append(("equation", "Eq.", match.group(1)))
        for match in _SECTION_REF_RE.finditer(text):
            refs.append(("section", "Section", match.group(1)))
        unique: list[tuple[str, str, str]] = []
        for ref in refs:
            if ref not in unique:
                unique.append(ref)
        return unique[:10]

    @staticmethod
    def _find_table(document: ParsedDocument, number: str) -> str:
        normalized = number.upper()
        for section in document.sections:
            for table in section.tables:
                caption = table.caption or ""
                match = re.search(r"(?:Table|TABLE|表)\s*([IVXLC0-9]+)", caption, re.I)
                if match and match.group(1).upper() == normalized:
                    return f"{caption}\n{table.markdown}".strip()
        # Fallback: numbered table in any paragraph
        for _sec, _index, text in document.iter_paragraphs():
            if not text.lstrip().startswith("|"):
                continue
            first = text.splitlines()[0] if text.splitlines() else ""
            if normalized in first.upper():
                return text
        return ""


def bundle_hash_from_text(text: Any) -> str:
    """Normalise the exact prompt text used as L3 evidence and hash it."""
    canonical = re.sub(r"\s+", " ", str(text or "")).strip().lower()
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def bundle_hash(bundle: ContextBundle | dict[str, Any]) -> str:
    """Canonical SHA256 fingerprint for a ContextBundle.

    The stored L3 evidence is ``ContextBundle.prompt_markdown()``; hashing that
    same text makes legacy evidence rows backfillable and lets incremental
    reprocessing skip already-extracted slices without another LLM call.
    """
    if isinstance(bundle, ContextBundle):
        return bundle_hash_from_text(bundle.prompt_markdown())
    if isinstance(bundle, dict):
        try:
            return bundle_hash_from_text(ContextBundle.model_validate(bundle).prompt_markdown())
        except Exception:
            return bundle_hash_from_text(
                "\n".join(str(bundle.get(key) or "") for key in sorted(bundle))
            )
    return bundle_hash_from_text("")


compute_bundle_hash = bundle_hash


def build_bundles(
    document: ParsedDocument,
    radar: RadarResult,
    *,
    doc_id: str | None = None,
    max_bundles: int = 200,
    manual: bool = False,
) -> list[ContextBundle]:
    return ContextBundler().build(
        document, radar, doc_id=doc_id, max_bundles=max_bundles,
        manual=manual,
    )
