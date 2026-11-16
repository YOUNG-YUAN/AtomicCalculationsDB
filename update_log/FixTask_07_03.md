# FixTask_07_03.md — 结构化适用条件与 HTML 表格统一 TableBlock

## 1. 结构化适用条件

### 模型

`app_core/models/atomic_prompts.py` 新增 `CardConditions`：

- `target_quantity`
- `unit`
- `value`
- `uncertainty`
- `state_initial`
- `state_final`
- `transition`
- `j_parity`
- `method_boundary`
- `convergence`
- `applicability`
- `unknown_fields`

`KnowledgeCardDraft` 新增 `conditions` 字段，默认全部为空；提取 prompt 增加第 7 条规则：

- 未报告填 `unknown`；
- 明确不适用填 `not_applicable`；
- 无法判断的字段放入 `unknown_fields`；
- 不允许把未知条件留空后解释成“任意情况均适用”。

### 存储

不新增数据库列，条件写入现有 `structured_details_json` 的 `conditions` 键：

```json
{
  "conditions": {
    "target_quantity": "energy",
    "unit": "cm^-1",
    "unknown_fields": ["j_parity"]
  }
}
```

这样不会改变现有 `content_hash` 字段集合，也不需要给 3,882 张已有卡做全库哈希迁移。

### 展示与审计

- 卡片详情新增“结构化适用条件”区块；
- AI 审计报告新增“结构化适用条件”段；
- AI 审计的 `revised_card.conditions` 会合并回 `structured_details_json`。

## 2. HTML 表格统一 TableBlock

### 新解析器

新增 `app_core/parser/html_table.py`：

- `html_table_to_markdown()`：把 HTML 表格展平成 Markdown 管道表；
  - `rowspan` 在每一行重复；
  - `colspan` 只保留首列文本；
  - HTML 实体自动解码。
- `html_table_cells()`：保留原始单元格结构：
  - `row` / `col`
  - `text`
  - `header`
  - `rowspan` / `colspan`

### TableBlock 扩展

`TableBlock` 新增：

- `html`：原始 HTML；
- `cells`：结构化单元格；
- `header_rows`；
- `source_format`：`html` / `markdown`。

### 解析接入

`PDFParser.parse_markdown()` 现在识别 `<table>...</table>`：

- 支持单行和多行 HTML 表格；
- 生成真正的 `TableBlock`，不再当普通段落；
- `parsed.md` 中把 HTML 表格替换为管道表，同时保留 `<!-- ACDB-PAGE -->` 页码标记；
- `section.tables` 和四通道雷达 Channel B 都能命中；
- 原始 HTML 保留在 `TableBlock.html`，MinerU 缓存中的 `structured_content.json` 也继续保留。

### 兼容性

- PyMuPDF 表格仍走原有管道表路径；
- `TableBlock.source_format` 默认为 `markdown`；
- `cells` 和 `html` 暂不写入 `parsed.md`，只存在于解析期对象；后续可持久化到 `parsed_tables.json`。

## 3. 测试

新增 `tests/test_v32.py`：

- HTML 表格 `rowspan` / `colspan` 展平；
- 审计修订可以更新 `conditions`；
- `TableBlock` 保留原始 HTML 和单元格跨度；
- `parsed.md` 不再含 `<table>`；
- Channel B 命中 `STRATEGY_TABLE`；
- 结构化 conditions 写入 `structured_details_json`；
- 审计报告渲染适用条件。

全量回归：**149 passed**。

## 4. 未完成

- 逐块证据 locator（block_id / 字符范围 / bbox / 表格行列/表头/脚注）；
- `FigureBlock` 与图注通道；
- 处理台账；
- `cells` 持久化到独立表或 JSON；
- L1 总纲 stale 传播；
- `SUPPORTS/RELATED` 自动生成（继续保持不自动生成）。

本轮未构建发布包，也未覆盖安装目录。
