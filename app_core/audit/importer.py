"""Validate and apply external AI audit JSON without calling any model."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from db.card_hash import compute_card_content_hash
from parser.card_quality import sanitize_card_fields
from parser.consensus_conflict_engine import ConsensusConflictEngine

from .prompt import AUDIT_SCHEMA_NAME, verdict_for_score

log = logging.getLogger(__name__)

REVISABLE_FIELDS = {
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
    "generalized_conjecture",
}


class AuditImportError(ValueError):
    """Raised when an audit file cannot be safely interpreted."""


@dataclass
class AuditPreview:
    document_id: str
    report_id: str
    report_hash: str
    batch_id: str
    auditor: dict[str, Any]
    reviews: list[dict[str, Any]] = field(default_factory=list)
    new_cards: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)
    source_file: str = ""
    file_hash: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def review_by_id(self, knw_id: str) -> dict[str, Any] | None:
        for item in self.reviews:
            if str(item.get("knw_id")) == str(knw_id):
                return item
        return None


@dataclass
class AuditApplyResult:
    applied: int = 0
    modified: int = 0
    deleted: int = 0
    added: int = 0
    kept: int = 0
    needs_review: int = 0
    rejected: int = 0
    duplicates: int = 0
    warnings: list[str] = field(default_factory=list)


def _text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _extract_json(text: str) -> Any:
    body = str(text or "").strip()
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        pass
    for block in re.findall(r"```(?:json)?\s*(.+?)```", body, re.S | re.I):
        try:
            return json.loads(block.strip())
        except json.JSONDecodeError:
            continue
    decoder = json.JSONDecoder()
    for index, character in enumerate(body):
        if character not in "{[":
            continue
        try:
            value, _end = decoder.raw_decode(body[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, (dict, list)):
            return value
    raise AuditImportError("文件中没有可解析的 JSON 对象")


def load_audit_payload(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise AuditImportError(f"无法读取文件：{exc}") from exc
    payload = _extract_json(text)
    if not isinstance(payload, dict):
        raise AuditImportError("审计 JSON 顶层必须是对象")
    payload["_source_file"] = source.name
    payload["_file_hash"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return payload


def _normalize_action(value: Any) -> str:
    action = _text(value).lower()
    return action if action in ("keep", "modify", "delete") else "keep"


def _normalize_score(value: Any) -> int | None:
    try:
        score = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return max(0, min(100, score))


def _review_payload(
    entry: Mapping[str, Any],
    *,
    current: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    knw_id = _text(entry.get("knw_id")).upper()
    score = _normalize_score(entry.get("overall_score"))
    verdict = verdict_for_score(score) if score is not None else ""
    supplied_verdict = _text(entry.get("verdict"))
    if supplied_verdict and supplied_verdict != verdict:
        warnings.append(
            f"{knw_id}: verdict「{supplied_verdict}」与分数不一致，已按分数改为「{verdict}」"
        )
    action = _normalize_action(entry.get("suggested_action"))
    raw_revised = entry.get("revised_card") or {}
    if not isinstance(raw_revised, Mapping):
        raw_revised = {}
        warnings.append(f"{knw_id}: revised_card 不是对象，已忽略")
    conditions = raw_revised.get("conditions")
    revised = {
        key: value for key, value in raw_revised.items() if key in REVISABLE_FIELDS
    }
    if isinstance(conditions, Mapping):
        revised["conditions"] = dict(conditions)
    structured = entry.get("structured_details")
    if structured is not None and "structured_details_json" not in revised:
        revised["structured_details_json"] = structured
    keywords = entry.get("keywords")
    if keywords is not None:
        revised["keywords"] = keywords
    delete_reason = _text(entry.get("delete_reason"))
    if action == "modify" and not revised:
        warnings.append(f"{knw_id}: modify 没有可应用字段，已降级为 keep")
        action = "keep"
    if action == "delete" and not delete_reason:
        warnings.append(f"{knw_id}: delete 缺少理由，已降级为 keep")
        action = "keep"
    if score is None:
        warnings.append(f"{knw_id}: 缺少 overall_score，标记为无效")
    payload = {
        "knw_id": knw_id,
        "content_hash": _text(entry.get("content_hash")),
        "overall_score": score,
        "verdict": verdict,
        "confidence": entry.get("confidence"),
        "needs_manual_review": bool(entry.get("needs_manual_review")),
        "suggested_action": action,
        "revised_card": revised,
        "delete_reason": delete_reason,
        "issues": entry.get("issues") or [],
        "evidence": entry.get("evidence") or [],
        "raw": dict(entry),
    }
    current_hash = compute_card_content_hash(current)
    if not payload["content_hash"]:
        warnings.append(f"{knw_id}: 审计未提供 content_hash，按当前内容接受")
    elif payload["content_hash"] != current_hash:
        warnings.append(f"{knw_id}: content_hash 与当前卡片不一致，标记为过期")
    return payload, warnings


def preview_audit_payload(
    db: Any,
    payload: Mapping[str, Any],
    *,
    document_id: str | None = None,
    auditor_name: str = "",
    auditor_model: str = "",
    auditor_provider: str = "",
    auditor_node_id: str = "",
    audit_source: str = "manual_import",
) -> AuditPreview:
    """Validate all IDs/scores/actions before any database write."""
    schema = _text(payload.get("schema"))
    if schema and schema != AUDIT_SCHEMA_NAME:
        raise AuditImportError(f"不支持的审计 schema：{schema}")
    doc_id = _text(document_id or payload.get("document_id")).upper()
    if not doc_id or db.get_document(doc_id) is None:
        raise AuditImportError(f"找不到审计对应的文献：{doc_id or '—'}")
    report_id = _text(payload.get("report_id"))
    report_hash = _text(payload.get("report_hash"))
    batch_id = _text(payload.get("batch_id")) or "part-01"
    auditor = payload.get("auditor") or {}
    if not isinstance(auditor, Mapping):
        auditor = {}
    resolved_auditor = {
        "name": _text(auditor_name or auditor.get("name")) or "未命名审计 AI",
        "model": _text(auditor_model or auditor.get("model")),
        "provider": _text(auditor_provider or auditor.get("provider")),
        "node_id": _text(auditor_node_id or auditor.get("node_id")),
        "source": _text(audit_source) or "manual_import",
    }
    reviews = payload.get("card_reviews") or payload.get("reviews") or []
    if not isinstance(reviews, list):
        raise AuditImportError("card_reviews 必须是数组")
    new_cards = payload.get("new_cards") or []
    if not isinstance(new_cards, list):
        raise AuditImportError("new_cards 必须是数组")
    result = AuditPreview(
        document_id=doc_id,
        report_id=report_id,
        report_hash=report_hash,
        batch_id=batch_id,
        auditor=resolved_auditor,
        source_file=_text(payload.get("_source_file")),
        file_hash=_text(payload.get("_file_hash")),
        raw=dict(payload),
    )
    export_record = (
        db.audit_export_for_report(report_id) if report_id else None
    )
    if export_record is not None:
        if str(export_record.get("doc_id") or "").upper() != doc_id:
            raise AuditImportError("审计报告对应的文献与当前文献不一致")
        if report_hash and export_record.get("report_hash") != report_hash:
            raise AuditImportError("report_hash 与导出的报告不一致，请重新导出或重新审计")
    elif report_id:
        result.warnings.append(
            "未找到对应的本地导出记录；将依赖 knw_id/content_hash 做逐卡校验"
        )
    seen: set[str] = set()
    stats = {
        "ready": 0,
        "stale": 0,
        "missing": 0,
        "invalid": 0,
        "verified_pending": 0,
        "keep": 0,
        "modify": 0,
        "delete": 0,
        "new": 0,
        "duplicate": 0,
    }
    for entry in reviews:
        if not isinstance(entry, Mapping):
            result.warnings.append("忽略非对象的 card_review")
            continue
        knw_id = _text(entry.get("knw_id")).upper()
        if not knw_id:
            result.warnings.append("忽略缺少 knw_id 的审计项")
            stats["invalid"] += 1
            continue
        if knw_id in seen:
            result.warnings.append(f"{knw_id}: 重复出现，已忽略后一条")
            stats["invalid"] += 1
            continue
        seen.add(knw_id)
        current = db.get_knowledge_card(knw_id)
        if not current:
            result.warnings.append(f"{knw_id}: 数据库中不存在")
            stats["missing"] += 1
            result.reviews.append(
                {
                    "knw_id": knw_id,
                    "status": "missing",
                    "payload": dict(entry),
                    "current": {},
                }
            )
            continue
        item, warnings = _review_payload(entry, current=current)
        result.warnings.extend(warnings)
        current_hash = compute_card_content_hash(current)
        stale = bool(item["content_hash"] and item["content_hash"] != current_hash)
        invalid = item["overall_score"] is None
        verified = bool(int(current.get("is_user_verified") or 0))
        action = item["suggested_action"]
        if stale:
            status = "stale"
        elif invalid:
            status = "invalid"
        elif verified and action in ("modify", "delete"):
            status = "verified_pending"
        else:
            status = "ready"
        stats[status] = stats.get(status, 0) + 1
        stats[action] = stats.get(action, 0) + 1
        result.reviews.append(
            {
                "knw_id": knw_id,
                "status": status,
                "payload": item,
                "current": current,
                "current_hash": current_hash,
                "verified": verified,
            }
        )
    for index, entry in enumerate(new_cards, start=1):
        if not isinstance(entry, Mapping):
            result.warnings.append(f"新增卡 #{index}: 不是对象，已跳过")
            stats["invalid"] += 1
            continue
        card = entry.get("card") or entry.get("card_draft") or {}
        if not isinstance(card, Mapping):
            result.warnings.append(f"新增卡 #{index}: card 不是对象，已跳过")
            stats["invalid"] += 1
            continue
        draft = dict(card)
        draft["card_level"] = "L2"
        draft["supporting_doc_ids"] = [doc_id]
        draft["source_kind"] = "AI_AUDIT_ADDED"
        draft["is_user_verified"] = 0
        draft, _notes = sanitize_card_fields(draft)
        title = _text(draft.get("title_zh") or draft.get("title_en"))
        summary = _text(draft.get("summary_zh"))
        if not title or not (summary or _text(draft.get("premises_and_conditions"))):
            result.warnings.append(f"新增卡 #{index}: 缺少标题或摘要，已跳过")
            stats["invalid"] += 1
            continue
        duplicate = db.find_duplicate_card(doc_id, title_zh=title, summary_zh=summary)
        if duplicate:
            stats["duplicate"] += 1
            result.warnings.append(
                f"新增卡 #{index}: 与现有 {duplicate['knw_id']} 重复，已跳过"
            )
            continue
        result.new_cards.append(
            {
                "temp_id": _text(entry.get("temp_id")) or f"NEW-{index:02d}",
                "reason": _text(entry.get("reason")),
                "evidence": entry.get("evidence") or [],
                "card": draft,
            }
        )
        stats["new"] += 1
    result.stats = stats
    return result


def _review_id(preview: AuditPreview, knw_id: str) -> str:
    seed = (
        f"{preview.report_hash}|{knw_id}|{preview.auditor.get('name', '')}"
        .encode("utf-8")
    )
    return "REV-" + hashlib.sha256(seed).hexdigest()[:24]


def _revision_snapshot(card: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "knw_id",
        "card_level",
        "card_type",
        "title_zh",
        "title_en",
        "summary_zh",
        "premises_and_conditions",
        "element",
        "z_num",
        "ion_stage",
        "isoelectronic",
        "method",
        "code_name",
        "physical_process",
        "formulas_latex",
        "code_snippet",
        "pitfall_warning",
        "source_section",
        "page_num",
        "supporting_doc_ids_list",
    )
    return {key: card.get(key) for key in keys}


def _merge_revised_values(
    current: Mapping[str, Any],
    revised: Mapping[str, Any],
) -> tuple[dict[str, Any], list[Any]]:
    values: dict[str, Any] = {"knw_id": current["knw_id"]}
    keywords: list[Any] = []
    for key, value in revised.items():
        if key == "keywords":
            keywords = list(value or []) if isinstance(value, (list, tuple)) else []
            continue
        if key == "conditions" and isinstance(value, Mapping):
            structured = dict(values.get("structured_details_json") or current.get("structured") or {})
            structured["conditions"] = dict(value)
            values["structured_details_json"] = structured
            continue
        if key == "generalized_conjecture":
            conjecture = str(value).strip() if value else None
            values["generalized_conjecture"] = conjecture
            structured = dict(values.get("structured_details_json") or current.get("structured") or {})
            if conjecture:
                structured["generalized_conjecture"] = conjecture
            else:
                structured.pop("generalized_conjecture", None)
            values["structured_details_json"] = structured
            continue
        if key in REVISABLE_FIELDS:
            values[key] = value
    # Never allow the external audit to change these identity/ownership fields.
    values["card_level"] = current.get("card_level") or "L2"
    values["supporting_doc_ids"] = list(current.get("supporting_doc_ids_list") or [])
    values["is_user_verified"] = int(current.get("is_user_verified") or 0)
    values["is_vectorized"] = 0
    values["keywords"] = [item.get("term_en") for item in current.get("keywords") or [] if item.get("term_en")] + keywords
    return values, keywords


def _recompute_conflicts(db: Any, knw_ids: Iterable[str]) -> None:
    engine = ConsensusConflictEngine()
    for knw_id in dict.fromkeys(str(item) for item in knw_ids if item):
        card = db.get_knowledge_card(knw_id)
        if not card:
            continue
        filters = {
            "element": card.get("element"),
            "z_num": card.get("z_num"),
            "isoelectronic": card.get("isoelectronic"),
            "method": card.get("method"),
            "physical_process": card.get("physical_process"),
            "card_type": card.get("card_type"),
            "audit_hidden": 0,
        }
        filters = {key: value for key, value in filters.items() if value not in (None, "")}
        candidates = [
            item
            for item in db.list_knowledge_cards(filters=filters, limit=25)
            if item.get("knw_id") != knw_id
        ]
        if not candidates:
            continue
        result = engine.consolidate(card, candidates)
        db.upsert_knowledge_card(
            {
                "knw_id": knw_id,
                "consensus_count": max(1, int(result.consensus_count or 1)),
                "has_conflict": 1 if result.has_conflict else 0,
                "conflict_with_knw_ids": list(result.conflict_ids),
            }
        )


def apply_audit_payload(
    db: Any,
    preview: AuditPreview,
    *,
    selected_knw_ids: Iterable[str] | None = None,
    selected_new_ids: Iterable[str] | None = None,
    apply_verified: bool = False,
) -> AuditApplyResult:
    """Apply a validated preview; this function never calls an LLM."""
    selected = (
        None
        if selected_knw_ids is None
        else {str(item).upper() for item in selected_knw_ids}
    )
    selected_new = (
        None
        if selected_new_ids is None
        else {str(item) for item in selected_new_ids}
    )
    result = AuditApplyResult(warnings=list(preview.warnings))
    affected: list[str] = []
    for item in preview.reviews:
        knw_id = str(item.get("knw_id") or "").upper()
        if selected is not None and knw_id not in selected:
            continue
        status = str(item.get("status") or "")
        payload = dict(item.get("payload") or {})
        payload["review_id"] = _review_id(preview, knw_id)
        current = item.get("current") or {}
        if status in ("missing", "invalid", "stale"):
            result.rejected += 1
            if current:
                db.record_audit_review(
                    {
                        **payload,
                        **preview.auditor,
                        "doc_id": preview.document_id,
                        "report_id": preview.report_id,
                        "report_hash": preview.report_hash,
                        "batch_id": preview.batch_id,
                        "audit_source": preview.auditor.get("source", "manual_import"),
                        "status": "STALE" if status == "stale" else "REJECTED",
                        "validation_warning": "; ".join(result.warnings[-3:]),
                    }
                )
                db.refresh_audit_summary(knw_id)
            continue
        action = str(payload.get("suggested_action") or "keep")
        review_status = "APPLIED"
        if status == "verified_pending" and not apply_verified:
            review_status = "NEEDS_REVIEW"
            if action == "delete":
                db.mark_card_suggested_delete(
                    knw_id,
                    reason=payload.get("delete_reason", ""),
                    auditor=preview.auditor.get("name", ""),
                    hide=False,
                )
                result.needs_review += 1
            elif action == "modify":
                result.needs_review += 1
            db.record_audit_review(
                {
                    **payload,
                    **preview.auditor,
                    "doc_id": preview.document_id,
                    "report_id": preview.report_id,
                    "report_hash": preview.report_hash,
                    "batch_id": preview.batch_id,
                    "audit_source": preview.auditor.get("source", "manual_import"),
                    "status": "NEEDS_REVIEW",
                }
            )
            db.refresh_audit_summary(knw_id)
            continue

        if action == "modify":
            before = db.get_knowledge_card(knw_id) or current
            values, keywords = _merge_revised_values(before, payload.get("revised_card") or {})
            db.upsert_knowledge_card(values)
            if keywords:
                db.link_card_keywords(knw_id, keywords)
            after = db.get_knowledge_card(knw_id) or values
            db.add_card_revision(
                knw_id,
                audit_review_id=str(payload.get("review_id") or ""),
                before=_revision_snapshot(before),
                after=_revision_snapshot(after),
                applied_by=preview.auditor.get("name", "external_audit"),
            )
            db.set_card_audit_status(knw_id, "NORMAL", hidden=False)
            db.mark_cards_vector_pending([knw_id])
            db.enqueue_vector_tasks([knw_id])
            affected.append(knw_id)
            result.modified += 1
        elif action == "delete":
            db.mark_card_suggested_delete(
                knw_id,
                reason=payload.get("delete_reason", ""),
                auditor=preview.auditor.get("name", ""),
                hide=True,
            )
            result.deleted += 1
        else:
            result.kept += 1
        db.record_audit_review(
            {
                **payload,
                **preview.auditor,
                "doc_id": preview.document_id,
                "report_id": preview.report_id,
                "report_hash": preview.report_hash,
                "batch_id": preview.batch_id,
                "audit_source": preview.auditor.get("source", "manual_import"),
                "status": review_status,
                "applied_at": datetime.now().astimezone().isoformat(
                    timespec="seconds"
                ),
            }
        )
        db.refresh_audit_summary(knw_id)
        result.applied += 1

    for entry in preview.new_cards:
        temp_id = str(entry.get("temp_id") or "")
        if selected_new is not None and temp_id not in selected_new:
            continue
        card = dict(entry.get("card") or {})
        title = card.get("title_zh") or card.get("title_en")
        duplicate = db.find_duplicate_card(
            preview.document_id,
            title_zh=str(title or ""),
            summary_zh=str(card.get("summary_zh") or ""),
        )
        if duplicate:
            result.duplicates += 1
            result.warnings.append(
                f"{temp_id}: 与 {duplicate['knw_id']} 重复，未新增"
            )
            continue
        card["source_kind"] = "AI_AUDIT_ADDED"
        card["is_user_verified"] = 0
        card["is_vectorized"] = 0
        knw_id = db.upsert_knowledge_card(card)
        keywords = card.get("keywords") or []
        if keywords:
            db.link_card_keywords(knw_id, keywords)
        db.set_card_audit_status(knw_id, "AI_ADDED", hidden=False)
        db.record_audit_review(
            {
                "knw_id": knw_id,
                "doc_id": preview.document_id,
                "report_id": preview.report_id,
                "report_hash": preview.report_hash,
                "batch_id": preview.batch_id,
                "auditor_name": preview.auditor.get("name", ""),
                "auditor_model": preview.auditor.get("model", ""),
                "auditor_provider": preview.auditor.get("provider", ""),
                "auditor_node_id": preview.auditor.get("node_id", ""),
                "audit_source": preview.auditor.get("source", "manual_import"),
                "overall_score": None,
                "verdict": "审计新增",
                "suggested_action": "add",
                "raw_json": entry,
                "status": "APPLIED",
                "applied_at": datetime.now().astimezone().isoformat(
                    timespec="seconds"
                ),
            }
        )
        db.refresh_audit_summary(knw_id)
        db.mark_cards_vector_pending([knw_id])
        db.enqueue_vector_tasks([knw_id])
        affected.append(knw_id)
        result.added += 1

    if affected:
        _recompute_conflicts(db, affected)
    stats = {
        "applied": result.applied,
        "modified": result.modified,
        "deleted": result.deleted,
        "added": result.added,
        "kept": result.kept,
        "needs_review": result.needs_review,
        "rejected": result.rejected,
        "duplicates": result.duplicates,
    }
    db.record_audit_import(
        batch_id=preview.batch_id,
        report_id=preview.report_id,
        report_hash=preview.report_hash,
        auditor_name=preview.auditor.get("name", ""),
        file_name=preview.source_file,
        file_hash=preview.file_hash,
        stats=stats,
        applied=True,
    )
    return result
