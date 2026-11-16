"""Strict prompt and rating helpers for external AI card review."""

from __future__ import annotations

from typing import Any

AUDIT_SCHEMA_NAME = "acdb.audit.v1"

RATING_BANDS = (
    (90, 100, "完全相符"),
    (70, 89, "基本相符"),
    (50, 69, "大致相符"),
    (30, 49, "明显差异"),
    (10, 29, "基本否定"),
    (0, 9, "全盘否定"),
)


def verdict_for_score(score: Any) -> str:
    try:
        value = int(round(float(score)))
    except (TypeError, ValueError):
        return "大致相符"
    value = max(0, min(100, value))
    for low, high, label in RATING_BANDS:
        if low <= value <= high:
            return label
    return "大致相符"


def rating_band_rows() -> str:
    return "\n".join(
        f"| {low}–{high} | {label} |" for low, high, label in RATING_BANDS
    )


_AUDIT_PROMPT = """你是计算原子物理（MCDHF / RCI / RMBPT / CC / CI+MBPT / GRASP / FAC / AMBiT / DIRAC）领域的严格审稿人。

下面给你三样东西：
1. 原始文献 PDF，或该文献 OCR/解析后的 `source.md`；
2. 从该文献提取的《知识卡审计报告》；
3. 本提示词。

{source_note}

你的任务不是重写文献，而是逐张核对知识卡是否与 PDF 一致，并检查前提条件、公式、代码参数和物理结论是否可靠。

## 评分标准（每张卡单独评分，不是整篇报告）

| 分数 | 评级 |
|---|---|
{rating_table}

## 每张卡必须输出

- `knw_id`: 原样抄写报告中的 ID，不得修改；
- `content_hash`: 原样抄写报告中的内容指纹；
- `overall_score`: 0–100 整数；
- `verdict`: 六档评级之一；
- `confidence`: 0–1；
- `needs_manual_review`: 证据不足时为 true；
- `issues`: 问题数组，每项包含 severity、field、message、evidence；
- `suggested_action`: `keep` / `modify` / `delete`；
- `revised_card`: 仅当 `modify` 时给出需要修改的字段；
- `delete_reason`: 仅当 `delete` 时给出理由。

## 重要规则

1. 只依据 PDF/source.md 与报告，不使用外部知识臆造。
2. 必须覆盖本次报告/分片中的每一张卡；不能遗漏，不能编造 `knw_id`。
3. 每张卡只能出现一次。
4. 不要给整篇报告一个总体评分；每张卡都有自己的 `overall_score`。
5. `delete` 表示建议程序软隐藏该卡，不是物理删除；必须给出证据和 `delete_reason`。
6. `modify` 时 `revised_card` 只能包含要修改的内容字段，不要修改 `knw_id`、`card_level`、`supporting_doc_ids`。
7. 如果 PDF 中没有足够证据判断，`needs_manual_review=true`，`suggested_action=keep`，分数放在 50–69。
8. 如果发现报告缺失但 PDF 中确实存在的重要知识，放到顶层 `new_cards`，每条给 `temp_id`、`reason`、`evidence` 和完整 `card` 草稿。
9. 只输出一个严格 JSON 对象；不要 Markdown 代码围栏，不要解释文字，不要输出思考过程。
10. 只处理本次报告/分片列出的卡片；报告头中的 `batch_id` 必须原样回填。
11. 如果提供了 `source.md`，引用问题时尽量给出页码、章节和短引文；OCR 误识别处应以 PDF 为准。
12. 广义物理延拓 (`generalized_conjecture`) 的校对与补齐规范：
    - 若原卡已有延拓：重点校对该规律在原理上是否可行。若原理可行，直接保留，不要随意改写；若在原理上不可行，必须在 issues 中说明不可行的具体理由，并在 `revised_card` 中给出修正（或置为 null 取消该延拓）。
    - 若原卡未提供延拓：若该卡反映了规律或机理且确有推广价值，请遵循原子物理规律谨慎补齐；若纯属孤立数值或缺乏明确依据，坚决填 null。

{coverage_section}

## 顶层 JSON 结构

```json
{{
  "schema": "{schema}",
  "document_id": "{document_id}",
  "report_id": "{report_id}",
  "report_hash": "{report_hash}",
  "batch_id": "{batch_id}",
  "auditor": {{
    "name": "请填写你的名称/模型名",
    "model": "模型名",
    "provider": "provider"
  }},
  "card_reviews": [
    {{
      "knw_id": "KNW-...",
      "content_hash": "sha256:...",
      "overall_score": 82,
      "verdict": "基本相符",
      "confidence": 0.78,
      "needs_manual_review": false,
      "issues": [
        {{
          "severity": "minor",
          "field": "premises_and_conditions",
          "message": "缺少前提说明",
          "evidence": {{
            "page": 12,
            "section": "2. Computational details",
            "quote": "原文短引文"
          }}
        }}
      ],
      "suggested_action": "modify",
      "revised_card": {{
        "premises_and_conditions": "修正后的前提",
        "generalized_conjecture": "当 Z>30 时单粒子势主导，轨道弛豫效应随 Z^4 迅速弱化"
      }},
      "delete_reason": "",
      "evidence": []
    }}
  ],
  "new_cards": []
}}
```

本报告信息：
- document_id: `{document_id}`
- report_id: `{report_id}`
- report_hash: `{report_hash}`
- batch_id: `{batch_id}`
- 卡片数: `{card_count}`
- 原文形式: `{source_kind}`
- 覆盖审计: `{coverage_kind}`
"""


def build_audit_prompt(
    *,
    document_id: str,
    report_id: str,
    report_hash: str,
    batch_id: str,
    card_count: int,
    include_source: bool = False,
    coverage_audit: bool = False,
) -> str:
    source_note = (
        "本次已附带 `source.md`（OCR/解析 Markdown）。请以它与 PDF 交叉核对；"
        "source.md 可能有 OCR 误识别，发现问题时优先回看 PDF。"
        if include_source
        else "本次未附带 `source.md`；请直接以 PDF 为准。"
    )
    coverage_section = (
        """## 覆盖审计模式

本次任务除逐卡核对外，还要求检查“漏掉的知识”：

- 对照原文逐节检查现有卡片是否遗漏了独立的策略、模型、公式、数值、代码参数或坑点；
- 漏掉的内容放入顶层 `new_cards`，每项给出 `temp_id`、`reason`、`evidence` 和完整 `card` 草稿；
- `new_cards` 的标题/摘要必须与现有卡有可区分性，避免只换词重复；
- 现有卡片仍然逐张输出 `keep / modify / delete`，不要因为新增卡而跳过原卡；
- 证据至少包含页码、章节和短引文。"""
        if coverage_audit
        else ""
    )
    return _AUDIT_PROMPT.format(
        rating_table=rating_band_rows(),
        schema=AUDIT_SCHEMA_NAME,
        document_id=document_id,
        report_id=report_id,
        report_hash=report_hash,
        batch_id=batch_id,
        card_count=int(card_count),
        source_note=source_note,
        coverage_section=coverage_section,
        source_kind="source.md + PDF" if include_source else "PDF",
        coverage_kind="开启" if coverage_audit else "关闭",
    )
