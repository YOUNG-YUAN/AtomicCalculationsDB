"""Knowledge-card quality guards and formula sanitisation.

Older PDFs sometimes map radical/private glyphs to text such as ``vuuut1``;
a few model responses also copied an English explanation into
``formulas_latex``.  This module keeps those artefacts out of the database
and removes the "证据截断" placeholder cards produced for unreadable
fragments.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

TRUNCATED_MARKERS = (
    "证据截断",
    "无法可靠重建具体计算策略",
)
_MATH_MARKERS = re.compile(r"\\|=|\^|_|\{|\}|\d|[+\-*/]")
_ARTIFACT_RE = re.compile(r"\bvuuut\d*\b", re.I)
_SPACE_RE = re.compile(r"[ \t]+")


def is_invalid_card(card: Mapping[str, Any]) -> bool:
    text = " ".join(
        str(card.get(key) or "")
        for key in ("title_zh", "title_en", "summary_zh", "premises_and_conditions")
    )
    return any(marker in text for marker in TRUNCATED_MARKERS)


def clean_formula_field(value: Any) -> tuple[str, str]:
    """Return ``(formula, prose_notes)`` with PDF artefacts removed.

    ``vuuut1``-style private-font tokens are dropped.  Lines that contain no
    mathematical syntax but read like an English sentence are moved out of the
    formula field and returned as notes, so the caller can append them to the
    card summary instead.
    """

    body = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    body = _ARTIFACT_RE.sub("", body)
    lines: list[str] = []
    notes: list[str] = []
    for raw_line in body.split("\n"):
        line = _SPACE_RE.sub(" ", raw_line).strip()
        line = re.sub(
            r"^(?:The focus fragment contains|The focus fragment|This fragment contains)"
            r"\s*[:：,.-]?\s*",
            "",
            line,
            flags=re.I,
        ).strip()
        if not line:
            continue
        words = re.findall(r"[A-Za-z]{2,}", line)
        looks_like_prose = len(words) >= 2 and not _MATH_MARKERS.search(line)
        if looks_like_prose:
            notes.append(line)
        else:
            lines.append(line)
    formula = "\n".join(lines).strip()
    formula = re.sub(r"\n{2,}", "\n", formula).strip()
    return formula, " ".join(notes).strip()


def sanitize_card_fields(card: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    """Copy a card payload and sanitize its formula field."""
    result = dict(card)
    formula, notes = clean_formula_field(result.get("formulas_latex"))
    result["formulas_latex"] = formula
    if notes:
        summary = str(result.get("summary_zh") or "").strip()
        if notes not in summary:
            result["summary_zh"] = (summary + "\n\n" + notes).strip()
    return result, notes


def sanitize_existing_cards(
    db: Any,
    qdrant: Any = None,
    progress: Any = None,
    doc_ids: Any = None,
    should_stop: Any = None,
) -> dict[str, int]:
    """Clean legacy formula/prose artefacts already stored in the database."""
    cards = []
    if doc_ids is not None:
        clean_doc_ids = [str(did).strip().upper() for did in doc_ids if str(did).strip()]
        for did in clean_doc_ids:
            cards.extend(db.list_knowledge_cards(doc_id=did, limit=5000))
    else:
        offset = 0
        while True:
            batch = db.list_knowledge_cards(limit=500, offset=offset)
            if not batch:
                break
            cards.extend(batch)
            if len(batch) < 500:
                break
            offset += len(batch)
    updated = 0
    for index, summary_card in enumerate(cards, start=1):
        if should_stop and should_stop():
            break
        if progress is not None and (index == 1 or index % 100 == 0 or index == len(cards)):
            try:
                progress(index, len(cards))
            except Exception:
                pass
        original_formula = str(summary_card.get("formulas_latex") or "")
        cleaned_formula, formula_notes = clean_formula_field(original_formula)
        if cleaned_formula == original_formula and not formula_notes:
            continue
        full = db.get_knowledge_card(str(summary_card.get("knw_id") or ""))
        if not full:
            continue
        cleaned, _notes = sanitize_card_fields(full)
        if (
            cleaned.get("formulas_latex") == full.get("formulas_latex")
            and cleaned.get("summary_zh") == full.get("summary_zh")
        ):
            continue
        knw_id = str(full.get("knw_id") or "")
        updates = {
            "knw_id": knw_id,
            "formulas_latex": cleaned.get("formulas_latex") or "",
            "summary_zh": cleaned.get("summary_zh") or "",
            "keywords": [
                str(item.get("term_en"))
                for item in full.get("keywords") or []
                if item.get("term_en")
            ],
            "is_vectorized": 0,
        }
        db.upsert_knowledge_card(updates)
        if qdrant is not None:
            try:
                qdrant.delete(knw_id)
            except Exception:
                pass
        updated += 1
    if updated:
        db.log_event("INFO", "knowledge", f"净化公式伪影/说明文字 {updated} 张知识卡")
    return {"checked": len(cards), "updated": updated}


def purge_invalid_cards(
    db: Any,
    qdrant: Any = None,
    progress: Any = None,
    doc_ids: Any = None,
    should_stop: Any = None,
) -> dict[str, int]:
    """Delete legacy truncated/placeholder cards at startup.

    The operation is idempotent and also removes their Qdrant vectors.
    Returns ``{"checked": N, "deleted": M}``.
    """

    cards = []
    if doc_ids is not None:
        clean_doc_ids = [str(did).strip().upper() for did in doc_ids if str(did).strip()]
        for did in clean_doc_ids:
            cards.extend(db.list_knowledge_cards(doc_id=did, limit=5000))
    else:
        offset = 0
        while True:
            batch = db.list_knowledge_cards(limit=500, offset=offset)
            if not batch:
                break
            cards.extend(batch)
            if len(batch) < 500:
                break
            offset += len(batch)
    deleted = 0
    for index, card in enumerate(cards, start=1):
        if should_stop and should_stop():
            break
        if progress is not None and (index == 1 or index % 100 == 0 or index == len(cards)):
            try:
                progress(index, len(cards))
            except Exception:
                pass
        if not is_invalid_card(card):
            continue
        knw_id = str(card.get("knw_id") or "")
        if db.delete_knowledge_card(knw_id):
            deleted += 1
            if qdrant is not None:
                try:
                    qdrant.delete(knw_id)
                except Exception:
                    pass
    if deleted:
        db.log_event("INFO", "knowledge", f"清理证据截断/低质量知识卡 {deleted} 张")
    return {"checked": len(cards), "deleted": deleted}


__all__ = [
    "TRUNCATED_MARKERS",
    "clean_formula_field",
    "is_invalid_card",
    "purge_invalid_cards",
    "sanitize_card_fields",
    "sanitize_existing_cards",
]
