"""Export a complete, human-readable audit report for one document."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from db.card_hash import compute_card_content_hash

from .prompt import AUDIT_SCHEMA_NAME, build_audit_prompt

ROLE_LABELS = {
    "fast_llm": "快速提取",
    "slow_llm": "深度归纳",
    "vlm": "图像理解",
    "embedding": "向量模型",
    "reranker": "重排模型",
}


@dataclass
class AuditExportResult:
    export_dir: Path
    report_id: str
    report_hash: str
    card_count: int
    part_count: int
    files: list[Path]
    source_included: bool = False


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "document"))
    return cleaned[:80] or "document"


def _text(value: Any, limit: int | None = None) -> str:
    text = str(value or "").strip()
    if limit and len(text) > limit:
        return text[:limit] + "…"
    return text


def _code_block(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    fence = "```" if "```" not in text else "````"
    return f"{fence}\n{text}\n{fence}"


def _report_hash(
    doc: Mapping[str, Any],
    cards: Sequence[Mapping[str, Any]],
    *,
    source_sha256: str = "",
) -> str:
    payload = {
        "schema": AUDIT_SCHEMA_NAME,
        "document_id": doc.get("doc_id"),
        "source_sha256": source_sha256,
        "cards": sorted(
            (
                {
                    "knw_id": card.get("knw_id"),
                    "content_hash": card.get("content_hash"),
                }
                for card in cards
            ),
            key=lambda item: str(item["knw_id"]),
        ),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _front_matter(
    *,
    doc: Mapping[str, Any],
    report_id: str,
    report_hash: str,
    card_count: int,
    part: int,
    part_total: int,
    card_ids: Sequence[str],
) -> str:
    lines = [
        "---",
        f"schema: {AUDIT_SCHEMA_NAME}",
        f"report_id: {report_id}",
        f"report_hash: {report_hash}",
        f"document_id: {doc.get('doc_id', '')}",
        f"title: {json.dumps(_text(doc.get('title'), 300), ensure_ascii=False)}",
        f"authors: {json.dumps(_text(doc.get('authors_list') or doc.get('authors'), 300), ensure_ascii=False)}",
        f"journal: {json.dumps(_text(doc.get('journal'), 200), ensure_ascii=False)}",
        f"pub_year: {doc.get('pub_year') or ''}",
        f"doi: {json.dumps(_text(doc.get('doi'), 200), ensure_ascii=False)}",
        f"generated_at: {datetime.now().astimezone().isoformat(timespec='seconds')}",
        f"card_count: {card_count}",
        f"part: {part}",
        f"part_total: {part_total}",
        "card_ids_in_part:",
    ]
    lines.extend(f"  - {knw_id}" for knw_id in card_ids)
    lines.append("---")
    return "\n".join(lines)


def _provenance_lines(card: Mapping[str, Any]) -> list[str]:
    provenance = card.get("provenance") or {}
    lines = []
    for role, label in ROLE_LABELS.items():
        info = provenance.get(role) or {}
        if not info:
            lines.append(f"- {label}: 未记录")
            continue
        name = _text(info.get("node_name")) or _text(info.get("model")) or "未知节点"
        model = _text(info.get("web_model") or info.get("model"))
        node_type = _text(info.get("node_type"))
        detail = f"{name}"
        if model and model != name:
            detail += f" / {model}"
        if node_type:
            detail += f" / {node_type}"
        lines.append(f"- {label}: {detail}")
    return lines


def _audit_lines(card: Mapping[str, Any]) -> list[str]:
    reviews = card.get("audit_reviews") or []
    if not reviews:
        return ["- 暂无 AI 审计记录"]
    lines = []
    for review in reviews[:20]:
        lines.append(
            f"- {review.get('auditor_name') or '未命名 AI'}："
            f"{review.get('overall_score', '—')} 分 / "
            f"{review.get('verdict') or '—'} / "
            f"{review.get('suggested_action') or 'keep'}"
        )
        if review.get("delete_reason"):
            lines.append(f"  - 删除理由：{_text(review['delete_reason'], 300)}")
    return lines


def _render_card(
    card: Mapping[str, Any],
    *,
    include_history: bool,
    include_evidence: bool = True,
) -> str:
    keywords = [
        str(item.get("term_en") or "")
        for item in card.get("keywords") or []
        if item.get("term_en")
    ]
    lines = [
        f"## 卡片 {card.get('knw_id', '')}",
        "",
        f"- knw_id: {card.get('knw_id', '')}",
        f"- content_hash: {card.get('content_hash', '')}",
        f"- card_level: {card.get('card_level', 'L2')}",
        f"- card_type: {card.get('card_type', '')}",
        f"- audit_status: {card.get('audit_status', 'NORMAL')}",
        f"- 标题（中）: {_text(card.get('title_zh'))}",
        f"- 标题（英）: {_text(card.get('title_en'))}",
        f"- 摘要: {_text(card.get('summary_zh'))}",
        f"- 前提与边界: {_text(card.get('premises_and_conditions'))}",
        f"- 广义物理延拓: {_text(card.get('generalized_conjecture') or (card.get('structured') or {}).get('generalized_conjecture')) or '（未提供，若具推广价值且符合原子物理规律请补充）'}",
        f"- 元素 / Z / 离子态 / 等电子序列: "
        f"{_text(card.get('element'))} / {card.get('z_num') or '—'} / "
        f"{_text(card.get('ion_stage'))} / {_text(card.get('isoelectronic'))}",
        f"- 方法 / 程序 / 物理过程: "
        f"{_text(card.get('method'))} / {_text(card.get('code_name'))} / "
        f"{_text(card.get('physical_process'))}",
        f"- 结论标签: {card.get('conclusion_flags', 0)}",
        f"- 来源章节 / 页码: {_text(card.get('source_section'))} / "
        f"{card.get('page_num') if card.get('page_num') is not None else '—'}",
        f"- 支持文献 ID: {', '.join(card.get('supporting_doc_ids_list') or []) or '—'}",
        f"- 关键词: {', '.join(keywords) or '—'}",
        "",
        "### 生成模型来源",
        *_provenance_lines(card),
        "",
    ]
    conditions = (card.get("structured") or {}).get("conditions") or {}
    if isinstance(conditions, Mapping) and conditions:
        labels = (
            ("target_quantity", "目标量"),
            ("unit", "单位"),
            ("value", "数值"),
            ("uncertainty", "不确定度"),
            ("state_initial", "初态"),
            ("state_final", "末态"),
            ("transition", "跃迁类型"),
            ("j_parity", "J / 宇称"),
            ("method_boundary", "方法边界"),
            ("convergence", "收敛判据"),
            ("applicability", "适用范围"),
        )
        lines.extend(["### 结构化适用条件", ""])
        for key, label in labels:
            value = conditions.get(key)
            if value not in (None, ""):
                lines.append(f"- {label}: {_text(value, 500)}")
        unknown = conditions.get("unknown_fields") or []
        if unknown:
            lines.append(f"- 未知字段: {', '.join(str(item) for item in unknown)}")
        lines.append("")
    if card.get("formulas_latex"):
        lines.extend(["### 公式 LaTeX", _code_block(card["formulas_latex"]), ""])
    if card.get("code_snippet"):
        lines.extend(["### 代码片段", _code_block(card["code_snippet"]), ""])
    if card.get("pitfall_warning"):
        lines.extend(["### 排错提示", _text(card["pitfall_warning"]), ""])
    if include_evidence:
        evidence = card.get("evidence") or []
        if evidence:
            lines.extend(["### L3 原始证据", ""])
            for index, item in enumerate(evidence[:8], start=1):
                lines.extend(
                    [
                        f"#### 证据 {index}",
                        f"- 页码: {item.get('page_num') if item.get('page_num') is not None else '—'}",
                        f"- 章节: {_text(item.get('section_title'), 200) or '正文'}",
                        f"- bundle_hash: `{_text(item.get('bundle_hash'), 100)}`",
                        f"- locator: `{_text(json.dumps(item.get('locator') or {}, ensure_ascii=False), 600)}`",
                        "",
                        _code_block(_text(item.get("context_bundle_md"), 5000)),
                        "",
                    ]
                )
            if len(evidence) > 8:
                lines.extend(
                    [
                        f"> 另有 {len(evidence) - 8} 条证据未在 Markdown 报告中展开；"
                        "完整内容见 `cards.jsonl`。",
                        "",
                    ]
                )
    if include_history:
        lines.extend(["### 已有 AI 审计", *_audit_lines(card), ""])
    return "\n".join(lines)


def _machine_card(
    card: Mapping[str, Any],
    *,
    include_history: bool,
    include_evidence: bool = True,
) -> dict[str, Any]:
    fields = (
        "knw_id",
        "content_hash",
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
        "supporting_doc_ids_list",
        "audit_status",
        "audit_hidden",
        "source_kind",
    )
    result = {field: card.get(field) for field in fields}
    result["keywords"] = [
        str(item.get("term_en") or "")
        for item in card.get("keywords") or []
        if item.get("term_en")
    ]
    result["provenance"] = card.get("provenance") or {}
    if include_evidence:
        result["evidence"] = [
            {
                "chunk_id": item.get("chunk_id"),
                "doc_id": item.get("doc_id"),
                "section_title": item.get("section_title"),
                "page_num": item.get("page_num"),
                "bundle_hash": item.get("bundle_hash"),
                "locator": item.get("locator") or {},
                "context_bundle_md": _text(item.get("context_bundle_md"), 6000),
            }
            for item in (card.get("evidence") or [])[:20]
        ]
    if include_history:
        result["audit_reviews"] = card.get("audit_reviews") or []
    return result


def _chunk_blocks(
    blocks: Sequence[str],
    cards: Sequence[Mapping[str, Any]],
    *,
    max_cards: int,
    max_chars: int,
) -> list[list[tuple[str, Mapping[str, Any]]]]:
    chunks: list[list[tuple[str, Mapping[str, Any]]]] = []
    current: list[tuple[str, Mapping[str, Any]]] = []
    current_chars = 0
    max_cards = max(1, int(max_cards))
    max_chars = max(10_000, int(max_chars))
    for block, card in zip(blocks, cards):
        if current and (
            len(current) >= max_cards or current_chars + len(block) > max_chars
        ):
            chunks.append(current)
            current = []
            current_chars = 0
        current.append((block, card))
        current_chars += len(block)
    if current:
        chunks.append(current)
    return chunks or [[]]


def _write_readme(
    path: Path,
    *,
    report_id: str,
    part_count: int,
    source_included: bool = False,
    coverage_audit: bool = False,
    include_evidence: bool = True,
) -> None:
    source_line = (
        "2. source.md（该文献 OCR/解析后的 Markdown，已附带）；"
        if source_included
        else "2. 原文献 PDF（本包未附带 source.md，请手动提供 PDF）；"
    )
    coverage_line = (
        "本包启用了覆盖审计：除逐卡核对外，请重点找出原文中存在、"
        "但报告卡片没有覆盖的独立知识点，并写入顶层 new_cards。\n"
        if coverage_audit
        else ""
    )
    evidence_line = (
        "报告中包含每张卡的 L3 原始证据；cards.jsonl 中有完整机器可读证据。"
        if include_evidence
        else "本包未包含 L3 原始证据；如需核对上下文，请使用 source.md 或 PDF。"
    )
    text = f"""AtomicCalculationDB · AI 校对包

报告 ID: {report_id}
分片数量: {part_count}

请把以下文件一起交给外部 AI：
1. audit_prompt.md（或对应分片的 audit_prompt_part-*.md）；
{source_line}
3. report.md（或全部分片 report_part-*.md）。

{evidence_line}
{coverage_line}
外部 AI 必须返回严格 JSON。把 JSON 文件保存后，在程序的
“文献详情 → 导入 AI 校对结果”中导入。

注意：
- 导入不会调用大模型，不消耗 Token；
- “建议删除”只会软隐藏卡片，不会物理删除；
- 用户已确认的卡片不会被自动修改；
- 新增卡会标记为 AI_AUDIT_ADDED，默认未人工确认。
"""
    path.write_text(text, encoding="utf-8")


def export_audit_bundle(
    db: Any,
    workspace: str | Path,
    doc_id: str,
    *,
    include_history: bool = False,
    include_source: bool = True,
    include_evidence: bool = True,
    coverage_audit: bool = False,
    max_cards_per_part: int = 200,
    max_chars_per_part: int = 60_000,
) -> AuditExportResult:
    """Write report.md, audit_prompt.md, cards.jsonl and manifest.json."""
    doc = db.get_document(doc_id)
    if not doc:
        raise ValueError(f"文献不存在：{doc_id}")
    workspace_path = Path(workspace)
    source_path: Path | None = None
    source_sha256 = ""
    local_md = str(doc.get("local_md_path") or "").strip()
    if include_source and local_md:
        candidate = Path(local_md)
        if not candidate.is_absolute():
            candidate = workspace_path / candidate
        if candidate.is_file():
            source_path = candidate
            source_sha256 = hashlib.sha256(candidate.read_bytes()).hexdigest()
    raw_cards = db.cards_for_document(doc_id)
    cards: list[dict[str, Any]] = []
    for item in raw_cards:
        card = db.get_knowledge_card(str(item["knw_id"])) or dict(item)
        card["content_hash"] = compute_card_content_hash(card)
        cards.append(card)
    cards.sort(key=lambda item: (0 if item.get("card_level") == "L1" else 1,
                                 str(item.get("knw_id") or "")))
    report_hash = _report_hash(doc, cards, source_sha256=source_sha256)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    root = workspace_path / "exports" / "audit" / _safe_name(doc_id)
    export_dir = root / stamp
    counter = 1
    while export_dir.exists():
        export_dir = root / f"{stamp}-{counter:02d}"
        counter += 1
    export_dir.mkdir(parents=True, exist_ok=False)
    report_id = f"AUD-{doc_id}-{stamp}"
    files: list[Path] = []
    source_included = False
    if source_path is not None:
        target_source = export_dir / "source.md"
        shutil.copyfile(source_path, target_source)
        files.append(target_source)
        source_included = True

    blocks = [
        _render_card(
            card,
            include_history=include_history,
            include_evidence=include_evidence,
        )
        for card in cards
    ]
    chunks = _chunk_blocks(
        blocks,
        cards,
        max_cards=max_cards_per_part,
        max_chars=max_chars_per_part,
    )
    part_count = len(chunks)
    parts_meta: list[dict[str, Any]] = []

    for index, chunk in enumerate(chunks, start=1):
        part_card_ids = [str(card.get("knw_id") or "") for _block, card in chunk]
        batch_id = f"part-{index:02d}"
        part_text = "\n\n".join(block for block, _card in chunk)
        body = "\n".join(
            [
                _front_matter(
                    doc=doc,
                    report_id=report_id,
                    report_hash=report_hash,
                    card_count=len(cards),
                    part=index,
                    part_total=part_count,
                    card_ids=part_card_ids,
                ),
                "",
                f"# {_text(doc.get('title'), 300)} · 知识卡审计报告",
                "",
                f"> 分片 {index}/{part_count} · 本片 {len(part_card_ids)} 张卡 · "
                f"文献 {doc_id}",
                *(
                    ["> 原文：`source.md`（OCR/解析 Markdown，请与 PDF 交叉核对）"]
                    if source_included
                    else []
                ),
                "",
                part_text,
                "",
            ]
        )
        if part_count == 1:
            part_path = export_dir / "report.md"
        else:
            part_path = export_dir / f"report_part-{index:02d}.md"
        part_path.write_text(body, encoding="utf-8")
        files.append(part_path)
        prompt = build_audit_prompt(
            document_id=doc_id,
            report_id=report_id,
            report_hash=report_hash,
            batch_id=batch_id,
            card_count=len(part_card_ids),
            include_source=source_included,
            coverage_audit=coverage_audit,
        )
        prompt_name = (
            "audit_prompt.md"
            if part_count == 1
            else f"audit_prompt_part-{index:02d}.md"
        )
        prompt_path = export_dir / prompt_name
        prompt_path.write_text(prompt, encoding="utf-8")
        files.append(prompt_path)
        parts_meta.append(
            {
                "part": index,
                "batch_id": batch_id,
                "file": part_path.name,
                "prompt": prompt_path.name,
                "card_count": len(part_card_ids),
                "card_ids": part_card_ids,
            }
        )

    if part_count > 1:
        index_lines = [
            _front_matter(
                doc=doc,
                report_id=report_id,
                report_hash=report_hash,
                card_count=len(cards),
                part=0,
                part_total=part_count,
                card_ids=[],
            ),
            "",
            f"# {_text(doc.get('title'), 300)} · 知识卡审计报告索引",
            "",
            f"- 文献：`{doc_id}`",
            f"- 卡片总数：{len(cards)}",
            f"- 分片数：{part_count}",
            f"- report_hash：`{report_hash}`",
            f"- source.md：{'已附带' if source_included else '未附带'}",
            "",
            "## 分片",
        ]
        index_lines.extend(
            f"- `{part['file']}` · {part['batch_id']} · "
            f"{part['card_count']} 张卡 · 提示词 `{part['prompt']}`"
            for part in parts_meta
        )
        index_path = export_dir / "report.md"
        index_path.write_text("\n".join(index_lines) + "\n", encoding="utf-8")
        files.append(index_path)

    cards_path = export_dir / "cards.jsonl"
    with cards_path.open("w", encoding="utf-8") as handle:
        for card in cards:
            handle.write(
                json.dumps(
                    _machine_card(
                        card,
                        include_history=include_history,
                        include_evidence=include_evidence,
                    ),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            )
    files.append(cards_path)

    manifest = {
        "schema": AUDIT_SCHEMA_NAME,
        "report_id": report_id,
        "report_hash": report_hash,
        "document_id": doc_id,
        "title": doc.get("title") or "",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "card_count": len(cards),
        "part_count": part_count,
        "card_ids": [str(card.get("knw_id") or "") for card in cards],
        "parts": parts_meta,
        "files": [path.name for path in files],
        "include_history": bool(include_history),
        "include_evidence": bool(include_evidence),
        "coverage_audit": bool(coverage_audit),
        "source": {
            "included": source_included,
            "file": "source.md" if source_included else "",
            "sha256": source_sha256,
            "markdown_backend": (doc.get("metadata") or {}).get("markdown_backend", ""),
            "mineru_version": (doc.get("metadata") or {}).get("mineru_version", ""),
            "mineru_tier": (doc.get("metadata") or {}).get("mineru_tier", ""),
        },
    }
    manifest_path = export_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    files.append(manifest_path)
    readme_path = export_dir / "README.txt"
    _write_readme(
        readme_path,
        report_id=report_id,
        part_count=part_count,
        source_included=source_included,
        coverage_audit=coverage_audit,
        include_evidence=include_evidence,
    )
    files.append(readme_path)

    db.record_audit_export(
        report_id=report_id,
        doc_id=doc_id,
        report_hash=report_hash,
        card_count=len(cards),
        part_count=part_count,
        export_dir=str(export_dir),
        manifest=manifest,
    )
    return AuditExportResult(
        export_dir=export_dir,
        report_id=report_id,
        report_hash=report_hash,
        card_count=len(cards),
        part_count=part_count,
        files=files,
        source_included=source_included,
    )
