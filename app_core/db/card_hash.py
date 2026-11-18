"""Stable content fingerprints for knowledge-card audit round trips."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

CONTENT_HASH_FIELDS = (
    "card_type",
    "title_zh",
    "title_en",
    "summary_zh",
    "premises_and_conditions",
    "element",
    "z_num",
    "ion_stage",
    "isoelectronic",
    "shell_type",
    "method",
    "code_name",
    "physical_process",
    "conclusion_flags",
    "structured_details_json",
    "formulas_latex",
    "code_snippet",
    "pitfall_warning",
    "source_section",
    "page_num",
)


def _text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _structured(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    value = value if isinstance(value, Mapping) else {}
    return dict(value)


def canonical_card_content(card: Mapping[str, Any]) -> dict[str, Any]:
    """Return the immutable, audit-relevant card content.

    Search payload, model provenance, audit state and keyword links are
    deliberately excluded: they are derived or deliberately mutable without
    changing the actual conclusion being audited.
    """
    result: dict[str, Any] = {}
    for field in CONTENT_HASH_FIELDS:
        value = card.get(field)
        if field == "structured_details_json":
            value = _structured(value if value not in (None, "") else card.get("structured"))
        elif field == "conclusion_flags":
            try:
                value = int(value or 0)
            except (TypeError, ValueError):
                value = 0
        elif field == "z_num":
            try:
                v = int(value) if value not in (None, "") else None
                value = v if v is not None and 1 <= v <= 250 else None
            except (TypeError, ValueError, OverflowError):
                value = None
        elif field == "page_num":
            try:
                value = int(value) if value not in (None, "") else None
            except (TypeError, ValueError):
                value = None
        elif field == "card_type":
            value = _text(value).upper()
        else:
            value = _text(value)
        result[field] = value
    return result


def compute_card_content_hash(card: Mapping[str, Any]) -> str:
    canonical = canonical_card_content(card)
    payload = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()
