# FixTask_07_04.md — 证据 locator、FigureBlock 与解析台账

## 1. Schema v3.2

`raw_evidence_chunks` 新增：

```sql
locator_json TEXT NOT NULL DEFAULT '{}'
```

`SCHEMA_VERSION` 升为 **3.2**；旧数据库通过 `_ensure_columns()` 自动补齐。

## 2. 证据 locator v1

`ContextBundle` 新增 `locator` 字段，`ContextBundler` 在生成 bundle 时写入：

- `source = "radar_bundle"`
- `page_num`
- `section_path`
- `paragraph_index`
- `channels`
- `focal_sentence`
- `table` / `formula` / `code` / `figure`
- `figure_image`
- `markdown_backend`

`pipeline._store_draft()` 写入 L3 evidence 时补充：

- `doc_id`
- `pdf_sha256`
- `markdown_backend`

`SQLiteManager.add_evidence()` 接受 `locator`，写入 `locator_json`；`list_evidence()` / `get_evidence()` 返回解析后的 `locator`。

审计报告中每张卡的 L3 证据新增 locator；`cards.jsonl` 的 `evidence` 数组也包含 locator。

边界：这是 **locator v1**，精确到页/节/段落/通道，不是 block_id、字符范围或 bbox。后者需要 parser 在 block 级保留坐标，属于下一步。

## 3. FigureBlock 与图注通道

`pdf_to_markdown.py` 新增：

```python
@dataclass
class FigureBlock:
    caption: str
    image: str
    page_num: int | None
    bbox: tuple | None
```

- `Section` 新增 `figures`；
- `parse_markdown()` 识别 `![caption](image)`，生成 `FigureBlock`；
- `_MarkdownBuilder.figure()` 保留图片 Markdown 和 `Section.figures`；
- `FourChannelRadar` 新增 `FIGURE` 通道，对图注生成 `RadarHit`；
- `ContextBundler` 的 locator 记录 `figure` 和 `figure_image`。

当前只保留图注和图片引用，不做默认 VLM 图像理解；关键图仍可按需调用 VLM。

## 4. 解析台账与持久化

`Pipeline.parse_document()` 现在额外写出：

- `parsed_tables.json`：
  - section_path
  - caption
  - page_num
  - bbox
  - rows
  - source_format
  - header_rows
  - markdown
  - html
  - cells
- `parsed_figures.json`：
  - section_path
  - caption
  - image
  - page_num
  - bbox
- `parse_manifest.json`：
  - markdown_backend
  - page/section/paragraph/table/figure/formula/code 数量
  - radar_channels 计数
  - bundle_count
  - 每个 bundle 的 section/page/channels/locator
  - 三个派生文件名

这使 `TableBlock.cells` 和 `FigureBlock` 不再只存在于解析期对象，便于审计和后续重放。

## 5. 测试

`tests/test_v32.py` 新增：

- evidence locator 往返；
- 图注进入 `Section.figures` 和 `FIGURE` 通道；
- `parse_document` 写出 `parsed_tables.json`、`parsed_figures.json`、`parse_manifest.json`；
- 表格 cells / HTML / source_format 持久化。

全量回归：**152 passed**。

## 6. 仍未完成

- 精确 block_id / 字符范围 / bbox 级 locator；
- 表格行列/表头/脚注级的 evidence 定位；
- 雷达未命中/预算拒绝的处理台账（当前台账是解析产物统计，不是逐候选拒绝原因）；
- 图片内容理解（当前只保留图注/引用）；
- L1 `master_stale` 传播；
- `SUPPORTS / RELATED` 自动生成（继续保持不自动生成）；
- 发布包与安装目录未更新。
