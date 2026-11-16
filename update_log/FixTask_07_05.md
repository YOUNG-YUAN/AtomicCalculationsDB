# FixTask_07_05.md — PyMuPDF 块级 locator 与表格单元格元数据

## 1. SourceBlock

`app_core/parser/pdf_to_markdown.py` 新增：

```python
@dataclass
class SourceBlock:
    block_id: str
    page_num: int | None
    bbox: tuple[float, float, float, float] | None
    text: str
    char_start: int
    char_end: int
    kind: str
    meta: dict
```

`Section` 新增 `paragraph_blocks`，与 `paragraphs` 一一对应。

## 2. PyMuPDF 路径

每个页面按 y 顺序生成块编号：

```text
<DOC-ID>:p<page>:b<index>
```

文本块保存：

- block_id
- page_num
- bbox
- 原文
- char_start / char_end
- 字体大小/字体名

表格块保存：

- block_id
- page_num
- bbox
- caption
- source_format
- header_rows
- row_count / column_count
- cells（row / col / rowspan / colspan / header / text）

## 3. ContextBundle locator 扩展

`ContextBundle.locator` 新增：

- `block_id`
- `bbox`
- `block_kind`
- `block_char_start` / `block_char_end`
- `char_start` / `char_end`（焦点句在段落内的偏移）
- `table_caption`
- `table_source_format`
- `table_header_rows`
- `table_row_count`
- `table_column_count`
- `table_cells`（最多 500 个单元格）

L3 evidence 的 `locator_json` 会保存这些字段；审计报告和 `cards.jsonl` 都能看到。

## 4. Markdown 表格/图片

`parse_markdown()` 为 HTML/管道表格和图片生成稳定合成 ID：

```text
md:p<page>:t<index>
md:p<page>:f<index>
```

并修复表题被 pop 时 `paragraphs` 与 `paragraph_blocks` 的对齐问题。

## 5. 测试

`tests/test_v32.py` 新增：

- PyMuPDF 文本块有 block_id / bbox / char_end；
- ContextBundle locator 带 block_id / bbox / char offsets；
- HTML 表格 locator 带 table_cells / header_rows；
- 表题段落与块对齐。

全量回归：**153 passed**。

## 6. 边界

- MinerU 路径目前仍使用 Markdown 合成 block ID，没有把 `structured_content.json` 的原始 block/bbox 传进 `ContextBundle`；
- PyMuPDF 表格有表格级 bbox，单元格 bbox 尚未保存；
- 表格脚注、表头层级、跨页表头还未做；
- 卡片详情尚未提供“点击跳转到 PDF bbox”的 UI；
- 字符偏移是块文本内的偏移，不是 PDF 绝对字符流偏移。
