"""Document genre detection for program manuals and practical guides."""

from __future__ import annotations

import re
from typing import Iterable

_MANUAL_HINTS = (
    r"\bGRASP\d*\b",
    r"\bFAC\b",
    r"\bAMBiT\b",
    r"(?-i:\bDIRAC\b)",
    r"\bBSR\b",
    r"\bguide\b",
    r"\bmanual\b",
    r"\bpackage\b",
    r"\bprogram\b",
    r"Computer\s+Physics\s+Communications",
    r"\bversion\s+of\b",
)
_MANUAL_HINT_RE = re.compile("|".join(_MANUAL_HINTS), re.I)
_SKIP_SECTION_RE = re.compile(
    r"^\s*(?:\d+(?:\.\d+)*[.)]?\s*)?"
    r"(installation|compilation|compiling|directory\s+structure|"
    r"license|licence|acknowledg(?:e)?ments?|references?|"
    r"table\s+of\s+contents|contents|bibliography|index)\b.*$",
    re.I,
)
_LOW_VALUE_SEGMENT_RE = re.compile(
    r"\b(installation|compilation|compiling|directory\s+structure|"
    r"license|licence|acknowledg(?:e)?ments?|references?|"
    r"table\s+of\s+contents|contents|bibliography|index)\b",
    re.I,
)
_DOT_LEADER_RE = re.compile(r"(?:\.{5,}|…{3,})")


def is_manual_document(
    title: str = "",
    doc_type: str = "",
    filename: str = "",
    section_titles: Iterable[str] = (),
) -> bool:
    if str(doc_type or "").upper() == "MAN":
        return True
    probe = " ".join(
        [str(title or ""), str(filename or ""), *[str(item) for item in section_titles]]
    )
    return bool(_MANUAL_HINT_RE.search(probe))


def is_manual_skip_section(title: str) -> bool:
    return bool(_SKIP_SECTION_RE.match(str(title or "").strip()))


def is_low_value_bundle(
    section_path: str,
    focal_window_markdown: str = "",
    focal_sentence: str = "",
    *,
    manual: bool = True,
) -> bool:
    """Return True for manual bundles that are very unlikely to yield cards."""
    if not manual:
        return False
    path = str(section_path or "").strip()
    for segment in re.split(r"\s*>\s*|\s*\|\s*", path):
        if _LOW_VALUE_SEGMENT_RE.search(segment):
            return True
    text = str(focal_window_markdown or "")
    compact = re.sub(r"\s+", "", text)
    if not compact:
        return True
    # Table-of-contents pages are mostly dot leaders and page numbers.
    if _DOT_LEADER_RE.search(text) and len(
        re.findall(r"[A-Za-z一-鿿]", text)
    ) < 160:
        return True
    if "front matter" in path.lower() and len(compact) < 120:
        return True
    return False


__all__ = [
    "is_manual_document",
    "is_manual_skip_section",
    "is_low_value_bundle",
]
