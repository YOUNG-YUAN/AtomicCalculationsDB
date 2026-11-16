"""Deep Synthesis Service — Step 5 (L1 Master Card Synthesis) across Web AI, Codex & Antigravity.

Transforms L2 detail knowledge cards of a document into a synthesized L1 Master Card:
  1. Web AI (Clipboard Prompt Export & Import)
  2. Codex (Agent CLI via `codex exec` sandbox)
  3. Antigravity (Agent Workbench MCP tickets & tools)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from db.id_generator import generate_master_id
from models.agent_cli import run_agent_cli
from models.atomic_prompts import (
    MASTER_SYSTEM_PROMPT,
    MasterCardDraft,
    build_master_messages,
    parse_master_json,
)
from models.endpoint_pool import EndpointNode

log = logging.getLogger(__name__)


def export_master_synthesis_prompt(db: Any, doc_id: str) -> dict[str, Any]:
    """Export all L2 cards of a document into a high-density Markdown prompt for Web AI."""
    doc = db.get_document(doc_id)
    if not doc:
        raise ValueError(f"文献不存在: {doc_id}")

    cards = db.cards_for_document(doc_id)
    l2_cards = [
        c for c in cards
        if str(c.get("card_level") or "L2").upper() == "L2"
        and str(c.get("card_type") or "").upper() != "MASTER"
    ]

    if not l2_cards:
        raise ValueError(
            f"文献 [{doc_id}] 尚未生成 L2 细节卡片（目前 0 张）。请先完成前 4 步入库后再执行深度归纳。"
        )

    # Detect dominant method and isoelectronic sequence
    methods = [str(c.get("method") or "") for c in l2_cards if c.get("method")]
    iso_list = [str(c.get("isoelectronic") or "") for c in l2_cards if c.get("isoelectronic")]
    top_method = max(set(methods), key=methods.count) if methods else ""
    top_iso = max(set(iso_list), key=iso_list.count) if iso_list else ""

    messages = build_master_messages(
        l2_cards,
        isoelectronic=top_iso,
        method=top_method,
    )

    # Format user-friendly clipboard text
    schema_str = json.dumps(MasterCardDraft.model_json_schema(), ensure_ascii=False, indent=2)

    prompt_lines = [
        f"# 深度归纳任务 · 文献 [{doc_id}]",
        f"- 标题：{doc.get('title')}",
        f"- 年份：{doc.get('pub_year')} | 期刊：{doc.get('journal') or 'N/A'}",
        f"- 核心方法：{top_method or '多方法综合'} | 等电子序列：{top_iso or '通用'}",
        f"- 包含原子细节卡片：{len(l2_cards)} 张",
        "",
        "## 系统角色与要求",
        MASTER_SYSTEM_PROMPT,
        "",
        "## 待归纳的原子化 L2 知识卡清单",
    ]

    for i, c in enumerate(l2_cards, 1):
        prompt_lines.extend([
            f"### 卡片 {i}: [{c.get('knw_id')}] {c.get('title_zh')}",
            f"- 类型: {c.get('card_type')} | 方法: {c.get('method')} | 物理过程: {c.get('physical_process')}",
            f"- 前提与边界: {c.get('premises_and_conditions')}",
            f"- 核心结论: {c.get('summary_zh')}",
        ])
        if c.get("generalized_conjecture"):
            prompt_lines.append(f"- 广义物理延拓/推论: {c.get('generalized_conjecture')}")
        if c.get("formulas_latex"):
            prompt_lines.append(f"- 核心公式: {c.get('formulas_latex')}")
        if c.get("code_snippet"):
            prompt_lines.append(f"- 计算代码/脚本: {c.get('code_snippet')}")
        if c.get("pitfall_warning"):
            prompt_lines.append(f"- 避坑提示: {c.get('pitfall_warning')}")
        prompt_lines.append("")

    prompt_lines.extend([
        "## 输出要求与 Schema",
        "请对上述所有细节卡片进行跨卡深度综合归纳，提炼出顶层 L1 Master 主卡片（收敛路线图、推荐参数矩阵、关键争议与待解决问题）。",
        "必须输出严格符合以下 Schema 的单一 JSON 对象，不要添加任何 Markdown 围栏外的多余废话：",
        "```json",
        schema_str,
        "```",
    ])

    full_prompt = "\n".join(prompt_lines)

    return {
        "doc_id": doc_id,
        "document_title": doc.get("title", ""),
        "card_count": len(l2_cards),
        "method": top_method,
        "isoelectronic": top_iso,
        "prompt": full_prompt,
        "messages": messages,
    }


def apply_master_synthesis_payload(
    db: Any,
    qdrant: Any,
    doc_id: str,
    payload: str | dict[str, Any],
    *,
    source_kind: str = "DEEP_SYNTHESIS",
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate MasterCardDraft payload, insert L1 Master Card to SQLite/Qdrant, and light up GOLD glow!"""
    if isinstance(payload, str):
        draft = parse_master_json(payload)
    elif isinstance(payload, dict):
        draft = MasterCardDraft.model_validate(payload)
    else:
        raise ValueError("不支持的 payload 类型，必须为 JSON 字符串或 dict")

    doc = db.get_document(doc_id)
    if not doc:
        raise ValueError(f"文献不存在: {doc_id}")

    cards = db.cards_for_document(doc_id)
    l2_cards = [
        c for c in cards
        if str(c.get("card_level") or "L2").upper() == "L2"
        and str(c.get("card_type") or "").upper() != "MASTER"
    ]

    elements = [str(c.get("element") or "") for c in l2_cards if c.get("element")]
    z_nums = [c.get("z_num") for c in l2_cards if c.get("z_num") is not None]
    ion_stages = [str(c.get("ion_stage") or "") for c in l2_cards if c.get("ion_stage")]
    methods = [str(c.get("method") or "") for c in l2_cards if c.get("method")]
    codes = [str(c.get("code_name") or "") for c in l2_cards if c.get("code_name")]

    element = elements[0] if elements else None
    z_num = z_nums[0] if z_nums else None
    ion_stage = ion_stages[0] if ion_stages else None
    method = draft.method or (methods[0] if methods else "GENERAL")
    code_name = codes[0] if codes else None
    isoelectronic = draft.isoelectronic or (ion_stage or "GENERAL")

    topic = isoelectronic or element or "GENERAL"
    knw_id = generate_master_id(topic, method)
    # Ensure uniqueness if already exists for another topic/method
    existing = db.get_knowledge_card(knw_id)
    if existing and doc_id not in (existing.get("supporting_doc_ids_list") or []):
        knw_id = f"{knw_id}-{doc_id[-4:]}"

    # Construct deterministic provenance chain from database ground truth
    provenance_chain = []
    for c in l2_cards:
        loc = c.get("locator") or {}
        if isinstance(loc, str):
            try:
                loc = json.loads(loc)
            except Exception:
                loc = {}
        provenance_chain.append({
            "knw_id": c.get("knw_id"),
            "doc_id": doc_id,
            "title_zh": c.get("title_zh"),
            "source_section": c.get("source_section") or loc.get("section_path") or "",
            "page_num": c.get("page_num") or loc.get("page_num"),
            "method": c.get("method"),
            "table_caption": loc.get("table_caption") or "",
        })
    draft.provenance_chain = provenance_chain

    search_payload = (
        f"[类型:MASTER | 体系:{ion_stage or ''} ({isoelectronic}, Z={z_num or ''}) | 方法:{method} ({code_name or ''})]\n"
        f"[总揽标题]: {draft.title_zh}\n"
        f"[核心归纳]: {draft.summary_zh}\n"
        f"[普适共性规律]: {draft.unified_commonality or '无'}\n"
        f"[关键发现]: {'; '.join(draft.key_findings)}\n"
        f"[待解问题]: {'; '.join(draft.open_questions)}"
    )

    card_record = {
        "knw_id": knw_id,
        "card_level": "L1",
        "card_type": "MASTER",
        "element": element,
        "z_num": z_num,
        "ion_stage": ion_stage,
        "isoelectronic": isoelectronic,
        "method": method,
        "code_name": code_name,
        "title_zh": draft.title_zh,
        "summary_zh": draft.summary_zh,
        "premises_and_conditions": f"深度归纳自文献 [{doc_id}] 的 {len(l2_cards)} 张原子细节知识卡",
        "structured_details_json": json.dumps(
            {
                "unified_commonality": draft.unified_commonality,
                "bifurcations": draft.bifurcations,
                "key_findings": draft.key_findings,
                "open_questions": draft.open_questions,
                "provenance_chain": provenance_chain,
            },
            ensure_ascii=False,
        ),
        "supporting_doc_ids": [doc_id],
        "search_payload": search_payload,
        "consensus_count": len(l2_cards),
        "authority_score": 1.5,
        "recency_year": doc.get("pub_year"),
        "card_year": doc.get("pub_year"),
        "source_kind": source_kind,
    }

    with getattr(db, "transaction", lambda: _DummyCtx())():
        db.upsert_knowledge_card(card_record, generate_missing=False)
        db.log_event(
            "INFO",
            "stepper",
            f"文献 {doc_id} 深度归纳完成：生成 L1 Master 卡 {knw_id}，进度条点亮金光！",
            doc_id=doc_id,
        )

    # Vectorize to Qdrant
    if qdrant is not None:
        try:
            qdrant.upsert_card(
                card_record["knw_id"],
                card_record["search_payload"],
                metadata={
                    "card_level": "L1",
                    "card_type": "MASTER",
                    "method": method,
                    "isoelectronic": isoelectronic,
                    "doc_id": doc_id,
                },
            )
        except Exception:
            log.debug("Qdrant upsert failed for master card %s", knw_id, exc_info=True)

    return {
        "success": True,
        "doc_id": doc_id,
        "knw_id": knw_id,
        "title_zh": draft.title_zh,
        "glow": "GOLD",
    }


def synthesize_with_agent_cli(
    db: Any,
    qdrant: Any,
    workspace: Path | str,
    doc_id: str,
    endpoint_node: EndpointNode,
    *,
    timeout_sec: int = 180,
) -> dict[str, Any]:
    """Execute Step 5 via Codex or local Agent CLI in a sandboxed runner."""
    ws = Path(workspace)
    export_info = export_master_synthesis_prompt(db, doc_id)
    prompt_text = export_info["prompt"]

    try:
        reply_text = run_agent_cli(
            endpoint_node,
            prompt_text,
            workspace=ws,
            timeout_sec=timeout_sec,
        )
    except Exception as exc:
        raise RuntimeError(f"Codex / Agent CLI 深度归纳执行失败: {exc}") from exc

    return apply_master_synthesis_payload(
        db,
        qdrant,
        doc_id,
        reply_text,
        source_kind=f"AGENT_CLI:{endpoint_node.name}",
    )


class _DummyCtx:
    def __enter__(self):
        return self
    def __exit__(self, exc_type, exc_val, exc_tb):
        return False
