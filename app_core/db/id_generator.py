"""V2.0 deterministic identifier generation.

Format rules (UpdateTask_01 §1.1):

* ``DOC-{TYPE}-{YEAR}-{HASH6}``
* ``MASTER-{TOPIC}-{METHOD}`` (L1 overview card)
* ``KNW-{CARD_TYPE}-{YEAR}-{HASH6}`` (L2 knowledge card)
* ``KW-{HASH6}``
* ``EV-{HASH8}`` (raw evidence chunk; internal extension)

All hashes are derived from normalised content so the same entity gets the
same ID on every machine, which is what makes ``.acdb`` incremental merges
safe.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Iterable

DOC_TYPES = ("PAP", "BOK", "REV", "MAN")
CARD_TYPES = ("STRAT", "MODEL", "CODE", "PITFALL", "CONC")
L1_CARD_TYPES = ("MASTER",)

_DOC_TYPE_ALIASES = {
    "PAPER": "PAP",
    "ARTICLE": "PAP",
    "PAP": "PAP",
    "BOOK": "BOK",
    "BOK": "BOK",
    "REVIEW": "REV",
    "REV": "REV",
    "MANUAL": "MAN",
    "MAN": "MAN",
}

_CARD_TYPE_ALIASES = {
    "STRAT": "STRAT",
    "STRATEGY": "STRAT",
    "MODEL": "MODEL",
    "CODE": "CODE",
    "PITFALL": "PITFALL",
    "CONC": "CONC",
    "CONCLUSION": "CONC",
    "MASTER": "MASTER",
}

_DOI_PREFIX_RE = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.I)
_WS_RE = re.compile(r"\s+")

DOC_ID_RE = re.compile(r"^DOC-(PAP|BOK|REV|MAN)-(\d{4})-([A-F0-9]{6})$")
KNW_ID_RE = re.compile(r"^KNW-(STRAT|MODEL|CODE|PITFALL|CONC)-(\d{4})-([A-F0-9]{6})$")
MASTER_ID_RE = re.compile(r"^MASTER-([A-Z0-9]+)-([A-Z0-9]+)$")
KW_ID_RE = re.compile(r"^KW-([A-F0-9]{6})$")
EV_ID_RE = re.compile(r"^EV-([A-F0-9]{8,16})$")

ANY_ID_RE = re.compile(
    r"(DOC-(?:PAP|BOK|REV|MAN)-\d{4}-[A-F0-9]{6}"
    r"|KNW-(?:STRAT|MODEL|CODE|PITFALL|CONC)-\d{4}-[A-F0-9]{6}"
    r"|MASTER-[A-Z0-9]+-[A-Z0-9]+"
    r"|KW-[A-F0-9]{6}"
    r"|EV-[A-F0-9]{8,16})",
    re.I,
)


class IdError(ValueError):
    """Raised when an identifier cannot be generated or parsed."""


def _sha(value: str, length: int) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length].upper()


def normalize_doi(doi: str | None) -> str:
    if not doi:
        return ""
    value = _DOI_PREFIX_RE.sub("", str(doi)).strip().lower()
    return value.rstrip(".,;)]}")


def normalize_title(title: str | None) -> str:
    """Lowercase and remove all whitespace (per the ID specification)."""
    if not title:
        return ""
    text = unicodedata.normalize("NFKC", str(title)).lower()
    return _WS_RE.sub("", text)


def normalize_hash_source(doi: str | None = None, title: str | None = None) -> str:
    """Return the canonical hash source: DOI first, title second."""
    normalized_doi = normalize_doi(doi)
    if normalized_doi:
        return f"doi:{normalized_doi}"
    normalized_title = normalize_title(title)
    if normalized_title:
        return f"title:{normalized_title}"
    raise IdError("生成 DOC-ID 需要 DOI 或标题")


def normalize_year(year: int | str | None, default: str = "0000") -> str:
    if year in (None, ""):
        return default
    match = re.search(r"(\d{4})", str(year))
    return match.group(1) if match else default


def _doc_type(doc_type: str | None) -> str:
    return _DOC_TYPE_ALIASES.get(str(doc_type or "PAP").upper(), "PAP")


def _card_type(card_type: str | None, *, level: str = "L2") -> str:
    value = _CARD_TYPE_ALIASES.get(str(card_type or "CONC").upper(), "CONC")
    if level.upper() == "L1" and value not in L1_CARD_TYPES:
        return "MASTER"
    return value


def normalize_doc_type(value: str | None) -> str:
    return _doc_type(value)


def normalize_card_type(value: str | None, *, level: str = "L2") -> str:
    return _card_type(value, level=level)


def generate_doc_id(
    doc_type: str | None,
    year: int | str | None,
    *,
    doi: str | None = None,
    title: str | None = None,
) -> str:
    source = normalize_hash_source(doi=doi, title=title)
    return f"DOC-{_doc_type(doc_type)}-{normalize_year(year)}-{_sha(source, 6)}"


def generate_knw_id(
    card_type: str,
    year: int | str | None,
    *,
    title: str | None = None,
    summary: str | None = None,
    supporting_doc_ids: Iterable[str] | None = None,
    level: str = "L2",
) -> str:
    kind = _card_type(card_type, level=level)
    if kind == "MASTER":
        raise IdError("L1 总纲卡请使用 generate_master_id()")
    docs = sorted(str(item).strip().upper() for item in (supporting_doc_ids or []) if item)
    source = "|".join(
        (
            kind,
            normalize_title(title),
            normalize_title(summary),
            ",".join(docs),
        )
    )
    if not normalize_title(title) and not normalize_title(summary):
        raise IdError("生成 KNW-ID 需要标题或摘要")
    return f"KNW-{kind}-{normalize_year(year)}-{_sha(source, 6)}"


def _sanitize_component(value: str | None, default: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()
    return cleaned or default


def generate_master_id(topic: str | None, method: str | None) -> str:
    topic_part = _sanitize_component(topic, "GENERAL")
    method_part = _sanitize_component(method, "GENERAL")
    return f"MASTER-{topic_part}-{method_part}"


def generate_keyword_id(term_en: str) -> str:
    canonical = _WS_RE.sub(" ", str(term_en or "").strip().lower())
    if not canonical:
        raise IdError("关键词不能为空")
    return f"KW-{_sha(canonical, 6)}"


def generate_evidence_id(
    parent_knw_id: str,
    doc_id: str,
    page_num: int | None,
    context_text: str,
) -> str:
    source = f"{parent_knw_id}|{doc_id}|{page_num}|{context_text}"
    return f"EV-{_sha(source, 10)}"


def classify_id(value: str | None) -> dict[str, str] | None:
    text = str(value or "").strip().upper()
    if not text:
        return None
    match = DOC_ID_RE.match(text)
    if match:
        return {"family": "document", "type": match.group(1), "id": text}
    match = KNW_ID_RE.match(text)
    if match:
        return {"family": "knowledge", "type": match.group(1), "id": text}
    match = MASTER_ID_RE.match(text)
    if match:
        return {"family": "master", "type": "MASTER", "id": text}
    match = KW_ID_RE.match(text)
    if match:
        return {"family": "keyword", "type": "KW", "id": text}
    match = EV_ID_RE.match(text)
    if match:
        return {"family": "evidence", "type": "EV", "id": text}
    return None


def looks_like_id(value: str | None) -> bool:
    return classify_id(value) is not None


def extract_ids(text: str | None) -> list[str]:
    if not text:
        return []
    return [match.group(1).upper() for match in ANY_ID_RE.finditer(text)]
