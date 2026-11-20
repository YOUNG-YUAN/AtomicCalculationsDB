"""Four-channel CPU probe.

Channel A  conclusion / pitfall sentences (spaCy, regex fallback)
Channel B  strategy tables vs. bulk data tables
Channel C  formulas and their definition context
Channel D  program workflows / code parameters

The radar never calls an LLM.  It produces compact, high-density evidence
windows that are later stitched by ``context_bundler``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from parser.conclusion_tagger import tag_text
from parser.document_profile import is_manual_document, is_manual_skip_section
from parser.pdf_to_markdown import ParsedDocument, Section, TableBlock

log = logging.getLogger(__name__)

_SPACY = None
_SPACY_TRIED = False
_SENTENCE_RE = re.compile(r"(?<=[.!?。！？])\s+|\n+")

_TABLE_REF_RE = re.compile(r"\b(?:Table|TABLE|Tab\.|Fig\.?|Figure|图)\s*[IVXLC0-9]+", re.I)
_COGNITIVE_RE = re.compile(
    r"\b(demonstrat(?:e|es|ed|ing)|indicat(?:e|es|ed|ing)|"
    r"reduc(?:e|es|ed|ing)|improv(?:e|es|ed|ing)|converg(?:e|es|ed|ing)|"
    r"agree(?:s|d|ment)?|discrepan(?:cy|cies)|outperform(?:s|ed)?|"
    r"show(?:s|ed|n)?|reveal(?:s|ed)?|found|observ(?:e|ed|es)|"
    r"underestimat(?:e|es|ed)|overestimat(?:e|es|ed))\b",
    re.I,
)
_CONTRAST_RE = re.compile(
    r"\b(However|Surprisingly|In contrast|Nevertheless|Nonetheless|"
    r"fail(?:s|ed)? to converge|variational collapse|intruder state|"
    r"cannot|unable to|discrepancy|disagreement|inconsistent)\b",
    re.I,
)
_CONDITION_RE = re.compile(
    r"\b(only when|only if|provided that|whereas|unless|if|when)\b", re.I
)
_PITFALL_RE = re.compile(
    r"(fail(?:s|ed)? to converge|variational collapse|intruder state|"
    r"numerical instability|diverg(?:e|ence)|not converge|"
    r"崩塌|不收敛|发散|闯入态|数值不稳定)",
    re.I,
)

_STRATEGY_TABLE_RE = re.compile(
    r"(model|active\s+space|active\s+set|csf|vv|cv|cc|convergence|comparison|"
    r"reference|configuration|basis|method|strategy|approximation|"
    r"策略|活性空间|收敛|组态|参考)",
    re.I,
)
_FORMULA_CONTEXT_RE = re.compile(
    r"(Hamiltonian|potential|operator|Breit|QED|polarization|matrix element|"
    r"where\s+.*\s+is|定义为|其中.*为|哈密顿|势函数|算符|矩阵元)",
    re.I,
)
_CODE_RE = re.compile(
    r"(rcsfgenerate|rnucleus|rcsfinteract|rncsl|rwfnestimate|rmcdhf|"
    r"rbiotransform|rtransition|rhfs|ris4|jj2lsj|"
    r"fac\.Structure|\brci\s*(?:<|>|--|\.inp|\.in\b)|"
    r"R_?max\s*=|Rmax\s*=|\bk\s*=|kappa\s*=|"
    r"\.inp\b|\.in\b|^\s*\$ |--[a-z])",
    re.I | re.M,
)

# Manuals are a distinct genre: worked examples, rules of thumb and input
# streams are valid knowledge even without experimental error comparisons.
_MANUAL_EXAMPLE_RE = re.compile(
    r"\b(example|case\s+study|sample\s+run|practical\s+guide|test\s+case|"
    r"illustrative\s+calculation)\b",
    re.I,
)
_MANUAL_PRACTICE_RE = re.compile(
    r"\b(in\s+practice|it\s+is\s+(?:advisable|important|recommended)\s+to|"
    r"layer\s+by\s+layer|hold\b.{0,60}\bfixed|spectroscopic\s+orbitals?|"
    r"correlation\s+orbitals?|to\s+avoid\s+convergence|weighting|"
    r"EOL|EAL|zero[- ]order\s+space)\b",
    re.I,
)
_MANUAL_WORKFLOW_RE = re.compile(
    r"\b(rnucleus|rcsfgenerate|rcsfinteract|rwfnestimate|rmcdhf|rci|"
    r"rbiotransform|rtransition|rhfs|ris4|jj2lsj)\b",
    re.I,
)
_MANUAL_SCRIPT_RE = re.compile(
    r"<<\s*EOF|\{2s,\s*2p\}|SD\s+excitations|input\s+stream|standard\s+input",
    re.I,
)
_NON_CONCLUSION_SECTION_RE = re.compile(
    r"\b(explanation of tables?|references?|bibliography|contents?|table of contents?|acknowledg(?:e)?ments?)\b",
    re.I,
)
_TABLE_CONTINUATION_OR_HEADER_RE = re.compile(
    r"^(?:table|tab\.|figure|fig\.)\s*[0-9ivxlc.]*(?:\s*\(cont[^\)]*\))?\.?\s*$",
    re.I,
)


@dataclass
class RadarHit:
    channel: str
    text: str
    section_path: str
    paragraph_index: int
    page_num: int | None = None
    sentence: str = ""
    caption: str = ""
    table_markdown: str = ""
    rows: int = 0
    tags_bitmask: int = 0
    tags: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class RadarResult:
    hits: list[RadarHit] = field(default_factory=list)
    conclusions: list[RadarHit] = field(default_factory=list)
    strategy_tables: list[RadarHit] = field(default_factory=list)
    bulk_tables: list[RadarHit] = field(default_factory=list)
    formulas: list[RadarHit] = field(default_factory=list)
    code_blocks: list[RadarHit] = field(default_factory=list)
    figures: list[RadarHit] = field(default_factory=list)

    def channels(self) -> list[str]:
        return sorted({hit.channel for hit in self.hits})

    def add(self, hit: RadarHit) -> None:
        self.hits.append(hit)
        if hit.channel in ("CONCLUSION", "PITFALL"):
            self.conclusions.append(hit)
        elif hit.channel == "STRATEGY_TABLE":
            self.strategy_tables.append(hit)
        elif hit.channel == "BULK_DATA_TABLE":
            self.bulk_tables.append(hit)
        elif hit.channel == "FORMULA":
            self.formulas.append(hit)
        elif hit.channel == "CODE":
            self.code_blocks.append(hit)
        elif hit.channel == "FIGURE":
            self.figures.append(hit)


def _get_nlp():
    global _SPACY, _SPACY_TRIED
    if _SPACY_TRIED:
        return _SPACY
    _SPACY_TRIED = True
    try:
        import spacy

        _SPACY = spacy.load("en_core_web_sm")
        _SPACY.max_length = 2_000_000
        log.info("spaCy en_core_web_sm loaded for sentence/dependency analysis")
    except Exception as exc:  # pragma: no cover - model optional
        log.info("spaCy unavailable (%s); using regex sentence splitter", exc)
        _SPACY = None
    return _SPACY


def split_sentences(text: str) -> list[str]:
    body = str(text or "").strip()
    if not body:
        return []
    if body.startswith(("```", "|", "$$")):
        return [body]
    nlp = _get_nlp()
    if nlp is not None:
        try:
            return [sentence.text.strip() for sentence in nlp(body).sents if sentence.text.strip()]
        except Exception:
            pass
    return [part.strip() for part in _SENTENCE_RE.split(body) if part.strip()]


def _has_condition_clause(text: str) -> bool:
    """Turn-6 protection: a conditional/subordinate clause is never detached."""
    if _CONDITION_RE.search(text):
        return True
    nlp = _get_nlp()
    if nlp is not None and len(text) < 800:
        try:
            doc = nlp(text)
            return any(token.dep_ in ("advcl", "relcl") for token in doc)
        except Exception:
            return False
    return False


def _numeric_ratio(table_markdown: str) -> float:
    rows = [
        line
        for line in table_markdown.splitlines()
        if line.strip().startswith("|") and "---" not in line
    ]
    if not rows:
        return 0.0
    numeric_rows = 0
    for row in rows:
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        numbers = sum(
            1
            for cell in cells
            if re.fullmatch(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", cell)
        )
        if numbers >= 2:
            numeric_rows += 1
    return numeric_rows / len(rows)


def _parse_markdown_tables(text: str) -> list[tuple[str, str, int]]:
    """Return ``(caption, markdown, data_rows)`` for tables in a paragraph."""
    lines = text.splitlines()
    tables: list[tuple[str, str, int]] = []
    index = 0
    while index < len(lines):
        if not lines[index].strip().startswith("|"):
            index += 1
            continue
        start = index
        while index < len(lines) and lines[index].strip().startswith("|"):
            index += 1
        block = "\n".join(lines[start:index])
        caption = ""
        if start > 0:
            previous = lines[start - 1].strip()
            if re.match(r"^(?:Table|TABLE|Tab\.|表)\s*[IVXLC0-9.]+", previous, re.I):
                caption = previous
        data_rows = max(0, len(block.splitlines()) - 2)
        tables.append((caption, block, data_rows))
    return tables


class FourChannelRadar:
    def __init__(self, *, max_sentences_per_section: int = 80):
        self.max_sentences_per_section = max_sentences_per_section

    def scan(self, document: ParsedDocument, *, manual: bool | None = None) -> RadarResult:
        if manual is None:
            manual = is_manual_document(
                title=document.title,
                section_titles=[section.title for section in document.sections],
            )
        result = RadarResult()
        for section in document.sections:
            if manual and is_manual_skip_section(section.title):
                continue
            if not manual and (
                _NON_CONCLUSION_SECTION_RE.search(section.title)
                or _NON_CONCLUSION_SECTION_RE.search(section.path)
            ):
                continue
            self._channel_a(section, result, manual=manual)
            self._channel_b(section, result)
            self._channel_c(section, result)
            self._channel_d(section, result)
            self._channel_figures(section, result)
        return result

    # -------------------------------------------------------------- channel A
    def _channel_a(self, section: Section, result: RadarResult, *, manual: bool = False) -> None:
        if not manual and (
            _NON_CONCLUSION_SECTION_RE.search(section.title)
            or _NON_CONCLUSION_SECTION_RE.search(section.path)
        ):
            return
        for paragraph_index, paragraph in enumerate(section.paragraphs):
            if paragraph.startswith(("```", "|", "$$")):
                continue
            sentences = split_sentences(paragraph)
            if manual:
                self._manual_hit(section, paragraph_index, paragraph, sentences, result)
            limit = len(sentences) if manual else self.max_sentences_per_section
            for idx, sentence in enumerate(sentences[:limit]):
                s_strip = sentence.strip()
                if not s_strip:
                    continue
                # Skip OCR bounding box JSON lines
                if "'bbox'" in s_strip or '{"bbox' in s_strip:
                    continue
                # Skip bare table continuation markers or bare figure headers
                if _TABLE_CONTINUATION_OR_HEADER_RE.match(s_strip):
                    continue
                is_table_ref = bool(_TABLE_REF_RE.search(sentence))
                is_cognitive = bool(_COGNITIVE_RE.search(sentence))
                is_contrast = bool(_CONTRAST_RE.search(sentence))
                if not (is_table_ref or is_cognitive or is_contrast):
                    continue
                # If only matching table_ref without cognitive/contrast cues, require a meaningful sentence
                if is_table_ref and not (is_cognitive or is_contrast):
                    words = re.findall(r"[A-Za-z0-9_-]+", s_strip)
                    if len(words) < 4 or len(s_strip) < 20:
                        continue
                window = sentence
                if is_table_ref and idx + 2 < len(sentences):
                    window = " ".join(sentences[idx : idx + 3])
                elif _has_condition_clause(sentence) and idx + 1 < len(sentences):
                    # keep the main clause together with the protected condition
                    window = sentence + " " + sentences[idx + 1]
                tags = tag_text(window)
                channel = "PITFALL" if _PITFALL_RE.search(window) else "CONCLUSION"
                result.add(
                    RadarHit(
                        channel=channel,
                        text=window,
                        section_path=section.path,
                        paragraph_index=paragraph_index,
                        page_num=section.page_start,
                        sentence=sentence,
                        tags_bitmask=tags.bitmask,
                        tags=tags.labels,
                        meta={"table_reference": is_table_ref},
                    )
                )

    def _manual_hit(
        self,
        section: Section,
        paragraph_index: int,
        paragraph: str,
        sentences: list[str],
        result: RadarResult,
    ) -> None:
        probe = paragraph
        cue = ""
        for pattern, name in (
            (_MANUAL_EXAMPLE_RE, "worked_example"),
            (_MANUAL_PRACTICE_RE, "practical_rule"),
            (_MANUAL_WORKFLOW_RE, "workflow"),
            (_MANUAL_SCRIPT_RE, "input_script"),
        ):
            if pattern.search(probe):
                cue = name
                break
        if not cue and paragraph_index == 0:
            for pattern, name in (
                (_MANUAL_EXAMPLE_RE, "worked_example"),
                (_MANUAL_PRACTICE_RE, "practical_rule"),
                (_MANUAL_WORKFLOW_RE, "workflow"),
                (_MANUAL_SCRIPT_RE, "input_script"),
            ):
                if pattern.search(section.title):
                    cue = name
                    break
        if not cue:
            return
        tags = tag_text(paragraph)
        if cue in ("workflow", "input_script") or _CODE_RE.search(paragraph):
            channel = "CODE"
        elif _PITFALL_RE.search(paragraph):
            channel = "PITFALL"
        else:
            channel = "CONCLUSION"
        result.add(
            RadarHit(
                channel=channel,
                text=paragraph,
                section_path=section.path,
                paragraph_index=paragraph_index,
                page_num=section.page_start,
                sentence=sentences[0] if sentences else paragraph,
                tags_bitmask=tags.bitmask,
                tags=tags.labels,
                meta={"manual_cue": cue, "manual_mode": True},
            )
        )

    # -------------------------------------------------------------- channel B
    def _channel_b(self, section: Section, result: RadarResult) -> None:
        tables: list[tuple[str, str, int, int | None, int]] = []
        if section.tables:
            for table in section.tables:
                paragraph_index = next(
                    (
                        index
                        for index, paragraph in enumerate(section.paragraphs)
                        if table.markdown and table.markdown in paragraph
                    ),
                    0,
                )
                tables.append(
                    (
                        table.caption,
                        table.markdown,
                        table.rows,
                        table.page_num,
                        paragraph_index,
                    )
                )
        else:
            for paragraph_index, paragraph in enumerate(section.paragraphs):
                if not paragraph.lstrip().startswith("|"):
                    continue
                for caption, markdown, rows in _parse_markdown_tables(paragraph):
                    tables.append(
                        (caption, markdown, rows, section.page_start, paragraph_index)
                    )
        for caption, markdown, rows, page_num, paragraph_index in tables:
            if not markdown.strip():
                continue
            header = self._table_header(markdown)
            probe = f"{caption} {header}"
            numeric = _numeric_ratio(markdown)
            is_non_conclusion_sec = bool(
                _NON_CONCLUSION_SECTION_RE.search(section.title)
                or _NON_CONCLUSION_SECTION_RE.search(section.path)
            )
            is_continuation = "(cont" in str(caption).lower()
            if rows >= 50 and numeric >= 0.55:
                kind = "BULK_DATA_TABLE"
            elif rows < 50 and _STRATEGY_TABLE_RE.search(probe) and not is_non_conclusion_sec and not is_continuation:
                kind = "STRATEGY_TABLE"
            else:
                kind = "DATA_TABLE"
            kept = not (
                kind == "BULK_DATA_TABLE"
                or is_non_conclusion_sec
                or is_continuation
                or (rows >= 30 and numeric >= 0.6)
            )
            result.add(
                RadarHit(
                    channel=kind,
                    text=f"{caption}\n{markdown}",
                    section_path=section.path,
                    paragraph_index=paragraph_index,
                    page_num=page_num,
                    caption=caption,
                    table_markdown=markdown,
                    rows=rows,
                    meta={"numeric_ratio": numeric, "kept_for_llm": kept},
                )
            )

    @staticmethod
    def _table_header(markdown: str) -> str:
        for line in markdown.splitlines():
            if line.strip().startswith("|") and "---" not in line:
                return line.strip("| ")
        return ""

    # ------------------------------------------------------------ figures
    def _channel_figures(self, section: Section, result: RadarResult) -> None:
        for figure in section.figures:
            caption = str(figure.caption or "").strip()
            if not caption:
                continue
            tags = tag_text(caption)
            result.add(
                RadarHit(
                    channel="FIGURE",
                    text=caption,
                    section_path=section.path,
                    paragraph_index=next(
                        (
                            index
                            for index, paragraph in enumerate(section.paragraphs)
                            if caption in paragraph
                        ),
                        0,
                    ),
                    page_num=figure.page_num or section.page_start,
                    sentence=caption,
                    tags_bitmask=tags.bitmask,
                    tags=tags.labels,
                    meta={"figure": figure.image},
                )
            )

    # -------------------------------------------------------------- channel C
    def _channel_c(self, section: Section, result: RadarResult) -> None:
        formulas: list[tuple[str, int]] = []
        for paragraph_index, paragraph in enumerate(section.paragraphs):
            for match in re.finditer(r"\$\$(.+?)\$\$", paragraph, re.S):
                formulas.append((match.group(1).strip(), paragraph_index))
        for formula in section.formulas:
            if not any(formula == item[0] for item in formulas):
                formulas.append((formula, 0))
        for formula, paragraph_index in formulas:
            previous = (
                section.paragraphs[paragraph_index - 1]
                if paragraph_index > 0
                else ""
            )
            following = (
                section.paragraphs[paragraph_index + 1]
                if paragraph_index + 1 < len(section.paragraphs)
                else ""
            )
            context = "\n\n".join(
                part for part in (previous, f"$$\n{formula}\n$$", following) if part
            )
            if not _FORMULA_CONTEXT_RE.search(context + " " + formula):
                continue
            result.add(
                RadarHit(
                    channel="FORMULA",
                    text=context,
                    section_path=section.path,
                    paragraph_index=paragraph_index,
                    page_num=section.page_start,
                    sentence=formula,
                    meta={
                        "previous": previous,
                        "formula": formula,
                        "next": following,
                    },
                )
            )

    # -------------------------------------------------------------- channel D
    def _channel_d(self, section: Section, result: RadarResult) -> None:
        seen: set[str] = set()
        for code in section.code_blocks:
            if code in seen:
                continue
            seen.add(code)
            paragraph_index = next(
                (
                    index
                    for index, paragraph in enumerate(section.paragraphs)
                    if code in paragraph
                ),
                0,
            )
            result.add(
                RadarHit(
                    channel="CODE",
                    text=f"```\n{code}\n```",
                    section_path=section.path,
                    paragraph_index=paragraph_index,
                    page_num=section.page_start,
                    sentence=code,
                    meta={"code": code},
                )
            )
        for paragraph_index, paragraph in enumerate(section.paragraphs):
            if paragraph.startswith("```") or paragraph in seen:
                continue
            if not _CODE_RE.search(paragraph):
                continue
            if len(paragraph) > 800:
                paragraph = paragraph[:800]
            if paragraph in seen:
                continue
            seen.add(paragraph)
            result.add(
                RadarHit(
                    channel="CODE",
                    text=paragraph,
                    section_path=section.path,
                    paragraph_index=paragraph_index,
                    page_num=section.page_start,
                    sentence=paragraph,
                    meta={"code": paragraph},
                )
            )
