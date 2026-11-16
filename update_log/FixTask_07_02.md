# FixTask_07_02.md — 审计包附带原文、L3 证据与覆盖审计

## 1. 目标

让外部 AI 不仅能逐张校对现有知识卡，还能拿着 OCR/解析后的原文找出被历史合并或漏掉的知识点。

## 2. 审计包新增内容

`app_core/audit/report.py`：

- `export_audit_bundle()` 新增参数：
  - `include_source=True`
  - `include_evidence=True`
  - `coverage_audit=False`
- 若文献存在 `local_md_path`，复制为 `source.md`，放入审计包；
- `report_hash` 现在包含 `source_sha256`，原文变化会使旧审计失效；
- `manifest.json` 新增：
  - `source.included`
  - `source.sha256`
  - `source.markdown_backend`
  - `source.mineru_version`
  - `source.mineru_tier`
  - `include_evidence`
  - `coverage_audit`
- `report.md` 每张卡新增 L3 原始证据（最多展开 8 条，完整内容在 `cards.jsonl`）；
- `cards.jsonl` 新增机器可读的 `evidence` 数组；
- `README.txt` 明确列出 `audit_prompt.md`、`source.md`/PDF、`report.md`。

## 3. 覆盖审计提示词

`app_core/audit/prompt.py`：

- 输入可以是 PDF 或 `source.md`；
- 提示词说明 OCR Markdown 可能有误识别，冲突时以 PDF 为准；
- `coverage_audit=True` 时增加“覆盖审计模式”：
  - 除逐卡核对外，检查原文中是否有未被卡片覆盖的独立知识点；
  - 漏掉的内容放入顶层 `new_cards`；
  - 每项给出 `temp_id`、`reason`、`evidence` 和完整卡片草稿；
  - 现有卡仍逐张输出 `keep / modify / delete`；
  - 证据至少包含页码、章节和短引文。

## 4. UI

文献详情 → AI 校对包新增三个选项：

- `附带 OCR/Markdown 原文`
- `报告包含 L3 证据`
- `覆盖审计（查漏卡）`

默认行为：

- 有 `local_md_path` 时默认附带原文；
- 默认附带 L3 证据；
- 覆盖审计默认关闭，避免每次审计都扩大范围。

## 5. 使用方式

```text
文献详情 → 导出 AI 校对包
  → 勾选“附带 OCR/Markdown 原文”
  → 按需勾选“覆盖审计（查漏卡）”
  → 把 audit_prompt.md + source.md/PDF + report.md 交给外部 AI
  → 导入 JSON
  → 在预览中勾选 new_cards 后应用
```

新增卡会标记为 `AI_AUDIT_ADDED`，默认未人工确认；审计导入不调用模型、不消耗 Token。

## 6. 测试

- `tests/test_v240.py` 新增：
  - 审计包包含 `source.md`；
  - `report.md` 包含 L3 证据；
  - `audit_prompt.md` 包含“覆盖审计模式”；
  - `manifest.source.sha256` 非空。
- 全量回归：**146 passed**。

## 7. 边界

- `source.md` 可能很大；当前仍按卡片分片，原文只附带一份，不按页切分；
- 覆盖审计可能产生近似重复的 `new_cards`；导入预览会按同文献标题/摘要做去重，但仍需人工勾选；
- 历史合并卡的原始 `knw_id`、原始模型来源无法恢复；AI 新增的是新的审计卡；
- 本轮仍未实现逐块证据 locator（block_id/bbox/表格行列），只附带现有 L3 bundle 文本。
