"""Long-book locator: ToC/index routing + dynamic keyword sniper.

For a 700-page monograph we never feed the whole book to an LLM.  Instead:

1. route on section titles / front-matter ToC and back-matter index;
2. score every section by the density of controlled strategy keywords;
3. return only the highest-density windows for extraction.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from db.sqlite_manager import SQLiteManager
from parser.pdf_to_markdown import PDFParser, ParsedDocument, Section

log = logging.getLogger(__name__)

DEFAULT_STRATEGY_KEYWORDS = (
    "correlation",
    "core-valence",
    "active space",
    "convergence",
    "breit",
    "qed",
    "configuration interaction",
    "basis set",
    "truncation",
    "extrapolation",
    "variational",
    "intruder",
    "gauge",
    "orbital",
    "csf",
    "rmbpt",
    "mcdhf",
    "rci",
)

_TOC_ROUTER_RE = re.compile(
    r"(numerical|computational|method|correlation|basis|convergence|"
    r"configuration|relativistic|many[- ]body|structure|approximation|"
    r"active\s+space|calculation)",
    re.I,
)
_ANGULAR_ONLY_RE = re.compile(
    r"(angular momentum algebra|recoupling|3-?j|6-?j|9-?j|racah|"
    r"spherical tensor|coefficient of fractional parentage)",
    re.I,
)


@dataclass
class BookWindow:
    section_path: str
    page_start: int | None
    page_end: int | None
    text: str
    density: float
    matched_keywords: list[str] = field(default_factory=list)
    score: float = 0.0
    routed: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "section_path": self.section_path,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "density": self.density,
            "matched_keywords": self.matched_keywords,
            "score": self.score,
            "routed": self.routed,
        }


class BookLocator:
    def __init__(self, db: SQLiteManager | None = None):
        self.db = db

    def keyword_list(self, extra: Iterable[str] | None = None) -> list[str]:
        terms = list(DEFAULT_STRATEGY_KEYWORDS)
        if self.db is not None:
            try:
                terms.extend(
                    keyword["term_en"] for keyword in self.db.list_keywords(limit=300)
                )
            except Exception:
                log.exception("Could not load controlled keywords")
        if extra:
            terms.extend(str(item) for item in extra)
        seen: set[str] = set()
        result: list[str] = []
        for term in terms:
            key = term.lower().strip()
            if key and key not in seen:
                seen.add(key)
                result.append(term)
        return result

    def locate(
        self,
        document: ParsedDocument,
        *,
        keywords: Iterable[str] | None = None,
        max_windows: int = 8,
    ) -> list[BookWindow]:
        terms = self.keyword_list(keywords)
        windows: list[BookWindow] = []
        total_pages = max(1, document.page_count or 1)
        for section in document.sections:
            text = section.text()
            if not text.strip():
                continue
            words = max(1, len(text.split()))
            matched = [term for term in terms if term.lower() in text.lower()]
            hits = sum(text.lower().count(term.lower()) for term in matched)
            # Floor the denominator and damp very short snippets so a 2-line
            # code block cannot outrank a real methods section.
            density = 1000.0 * hits / max(words, 150)
            density *= min(1.0, words / 80.0)
            routed = bool(_TOC_ROUTER_RE.search(section.title))
            if routed:
                density += 0.35
            if (
                _ANGULAR_ONLY_RE.search(text)
                and not any(
                    term.lower() in text.lower()
                    for term in ("correlation", "convergence", "active space", "breit", "qed")
                )
            ):
                density *= 0.25
            page_start = section.page_start or 0
            page_end = section.page_end or page_start
            if document.page_count:
                if page_start <= 15 or page_end >= total_pages - 20:
                    density += 0.15
            if density <= 0:
                continue
            windows.append(
                BookWindow(
                    section_path=section.path,
                    page_start=section.page_start,
                    page_end=section.page_end,
                    text=text,
                    density=round(density, 4),
                    matched_keywords=matched[:12],
                    score=round(density + math.log1p(hits) * 0.1, 4),
                    routed=routed,
                )
            )
        windows.sort(key=lambda item: item.score, reverse=True)
        return windows[: max(1, int(max_windows))]

    def locate_document(
        self,
        doc_id: str,
        *,
        max_windows: int = 8,
    ) -> list[BookWindow]:
        if self.db is None:
            raise RuntimeError("BookLocator 需要 SQLiteManager")
        document = self.db.get_document(doc_id)
        if not document:
            return []
        md_path = document.get("local_md_path")
        if not md_path:
            return []
        path = Path(str(md_path))
        if not path.is_absolute():
            path = self.db.workspace / path
        if not path.exists():
            return []
        markdown = path.read_text(encoding="utf-8")
        parsed = PDFParser().parse_markdown(
            markdown,
            title=document.get("title", ""),
            doc_id=document["doc_id"],
            page_count=0,
            source_path=str(path),
        )
        return self.locate(parsed, max_windows=max_windows)

    def enqueue_windows(
        self,
        doc_id: str,
        windows: Iterable[BookWindow],
        *,
        priority: int = 6,
    ) -> list[int]:
        if self.db is None:
            return []
        task_ids = []
        for window in windows:
            task_ids.append(
                self.db.add_task(
                    "EXTRACT",
                    doc_id=doc_id,
                    payload={
                        "section_path": window.section_path,
                        "page_start": window.page_start,
                        "page_end": window.page_end,
                        "keywords": window.matched_keywords,
                        "density": window.density,
                    },
                    priority=priority,
                )
            )
        return task_ids
