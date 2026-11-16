# FixTask_07_06.md — MinerU structured block/bbox 接入

## 1. MinerU blocks

`MinerUResult` 新增：

```python
blocks: list[dict[str, Any]]
```

`mineru_backend._blocks_from_structured()` 把 `structured_content.json` 展平为：

- `block_id`：`<DOC-ID>:p<page>:b<index>`
- `page_num`
- `block_index`
- `type`（doc_title / paragraph_title / text / table / equation / image / header / footer / page_number / ref_text / aside_text）
- `text`
- `markdown`
- `caption`
- `image_source`
- `bbox`（保留 MinerU 原始坐标）
- `cells` / `header_rows`（HTML 表格）
- `markdown` 为 HTML 表格时转成管道表供展示

## 2. 缓存

MinerU 缓存目录新增：

```text
blocks.json
```

`convert()` 写入；缓存命中时读取 `blocks.json`。旧缓存没有该文件时自动回退到 `page_markdown` 路径。

## 3. 直接按 block 构建 ParsedDocument

新增 `PDFParser.parse_mineru_blocks()`：

- 用 PDF 页尺寸把 MinerU 的归一化 bbox 转成 PDF 点坐标；
- `doc_title` → 一级标题；
- `paragraph_title` → 按编号推断二级/三级标题；
- `table` → `TableBlock`，保留 `bbox`、`html`、`cells`、`block_id`；
- `equation` → `FormulaBlock` 文本；
- `image` → `FigureBlock`，保留 `image_source` / `caption` / `bbox`；
- `text` → 普通段落，附 `SourceBlock`；
- `header/footer/page_number` 跳过。

`_finish_mineru_parse()` 现在优先使用 `result.blocks`；没有 blocks 时才回退到旧的 `page_markdown` 路径。

## 4. Locator

MinerU 路径现在也能给 ContextBundle 提供：

- `block_id`
- `bbox`
- `block_kind`
- `char_start` / `char_end`
- 表格 `cells` / `row_count` / `column_count` / `header_rows`

这样 MinerU 扫描件和 PyMuPDF 文本层 PDF 共用同一套 locator 模型。

## 5. 实机验证

用之前 23 页 Grant 1961 的 MinerU `structured_content.json`：

- 428 个 blocks
- 31 个 sections
- 344 个 paragraphs
- 8 个 tables
- 232 个带 SourceBlock 的段落
- 表格 block_id/bbox 正常
- 论文没有插图，FigureBlock 为 0

## 6. 测试

- `tests/test_v250.py` 新增 MinerU blocks 的 page/bbox/table cells 测试；
- 全量回归：**154 passed**。

## 7. 边界

- 表格 cells 目前只对 HTML 表格填充；MinerU 输出管道表时 cells 为空（已由 FixTask_07_07 补齐）；
- 单元格 bbox、表头层级、脚注仍未解析（PyMuPDF 单元格 bbox 已由 FixTask_07_07 补齐）；
- MinerU equation 暂未附 SourceBlock；
- 没有 PDF bbox 高亮跳转 UI（已由 FixTask_07_07 补齐）；
- 旧 MinerU 缓存没有 `blocks.json` 时仍走回退路径。
