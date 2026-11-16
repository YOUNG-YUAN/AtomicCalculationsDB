# FixTask_07_07.md — 表格单元格与 PDF 定位对话框

## 1. 表格 cells 统一

`app_core/parser/html_table.py` 新增：

```python
def table_header_rows(cells) -> int
```

按“连续且非空单元格全部为表头”的规则计算前置表头行数。HTML 表格、Markdown 管道表、MinerU 表格共用同一套 cells 结构：

```text
row / col / text / header / rowspan / colspan / bbox
```

## 2. MinerU 管道表

`_blocks_from_structured()` 现在同时支持：

- HTML 表格：`html_table_cells()`；
- 管道表：`markdown_table_cells()`。

`header_rows` 不再固定为 1，而是由 `table_header_rows()` 计算；`source_format` 按原始内容区分 `html` / `markdown`，修复了管道表被误标为 HTML 的问题。旧 `blocks.json` 缓存没有 cells 时仍可正常读取。

## 3. PyMuPDF 单元格 bbox

`page.find_tables()` 返回的 `Table.cells` 是列优先顺序：

```text
row = cell_index % row_count
col = cell_index // row_count
```

解析时把每个 cell bbox 写回对应行列，`source_format="pymupdf"`。`TableBlock.cells` 因此同时具备：

- 行/列索引；
- 文本；
- 表头标记；
- rowspan / colspan（当前 PyMuPDF 检测结果为 1/1）；
- 单元格 bbox。

## 4. PDF 原文定位

新增 `app_core/ui/pdf_locator.py`：

- `locator_has_bbox()` 判断证据是否具备页面和 bbox；
- `PdfLocatorDialog` 用 PyMuPDF 以 2 倍缩放渲染指定页；
- 在页面上绘制红色半透明 bbox；
- 卡片详情 L3 证据出现“定位原文”按钮；
- 找不到本地 PDF 或页面越界时给出提示/占位文本。

## 5. 测试

- `test_pipe_table_cells_are_structured`：管道表 cells；
- `test_pymupdf_table_cells_have_bboxes`：PyMuPDF 单元格 bbox；
- `test_pdf_locator_dialog_renders_page_and_bbox`：定位对话框冒烟；
- `test_structured_content_adapter_and_safe_zip`：MinerU 管道表 cells；
- 全量回归：**157 passed**。

## 6. 边界

- MinerU/HTML 表格没有单元格级 bbox，只有表格块 bbox；
- PyMuPDF 的 rowspan/colspan 目前通常为 1，合并单元格检测仍有限；
- 表头只识别前置连续表头行，复杂多级表头和表头层级仍未建模；
- 表格脚注仍未单独抽取；
- 图注仍只作为 FIGURE 通道文本，不做图像理解（按项目约束明确不引入 VLM 图像结论）；
- PDF 定位为只读预览，不做文本选择/复制。
