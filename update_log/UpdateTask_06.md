# UpdateTask_06.md — `AtomicCalculationDB` (V2.5.0) MinerU 4 全文献 OCR / Markdown 解析接入

> **目标**：把 MinerU 4.x 作为可选的“高质量 PDF → Markdown/结构化内容”后端接入手册/论文解析链路。扫描件、复杂双栏、表格、公式、图片较多的文献先由 MinerU 完整 OCR/版面解析，再进入现有的四通道雷达、三点式证据包和知识卡提取；MinerU 不可用时自动回退现有 PyMuPDF 解析。

---

## 1. 背景

当前 `app_core/parser/pdf_to_markdown.py` 使用 PyMuPDF 抽取文本块、表格和公式：

- 对原生 PDF 速度快、无外部依赖；
- 对扫描件、双栏、跨页表格、复杂公式效果有限；
- 扫描 PDF 目前会在 `Pipeline._text_layer_too_sparse()` 中直接失败，要求用户先做 OCR；
- 后续发给快速提取/深度归纳的 Markdown 质量，直接决定知识卡的准确性。

MinerU 4.x 提供：

- `flash / basic / standard / advanced` 四种解析档位；
- 原生 PDF 文本层提取与 OCR；
- Markdown、Middle JSON、ZIP、Structured Content、Content List 等输出；
- 本地文档库与无状态 CLI；
- CPU/GPU 自动选择，Windows x64 支持 DirectML/ONNX。

本任务不把 MinerU 及其模型打进主安装包（体积和驱动依赖不可控），而是作为**可选外部后端**：
自动检测、配置、调用、缓存、失败回退，并在 UI 中明确显示当前解析后端。

---

## 2. 设计原则

1. **可选，不强制**：默认关闭；用户安装 MinerU 后开启。
2. **不打包 MinerU/PyTorch/模型**：主程序保持轻量；MinerU 安装在自己的 Python 环境。
3. **无状态 CLI 优先**：调用 `mineru-kit parse`，不依赖 MinerU document library 服务、缓存和 token。
4. **失败必须回退**：超时、未安装、模型缺失、输出损坏时回退 PyMuPDF，并记录事件。
5. **缓存可复用**：同一 PDF + 相同 MinerU 版本/档位/OCR 模式只转换一次。
6. **保留页码/章节/表格/公式**：不能只拿一份丢失页码的 `full.md`；尽可能使用 `content_list_v2.json` 或 `middle_json.json` 恢复页码。
7. **不自动消耗云端资源**：MinerU 是本地/自托管工具；不调用云端 OCR API。
8. **可审计**：每篇文献记录 `markdown_backend`、MinerU 版本、档位、OCR 模式、输出目录和耗时。

---

## 3. MinerU 环境与命令

### 3.1 安装（外部）

推荐独立虚拟环境：

```bash
uv venv .mineru --python 3.12
# Windows
.mineru\Scripts\activate
uv pip install -U "mineru>=4.0,<5"
mineru version --json
```

首次使用某个档位可能需要下载模型：

```bash
mineru-kit models download --tier standard
# 或 basic / flash
```

### 3.2 实际调用

优先使用无状态单文件转换：

```bash
mineru-kit parse "<pdf>" -o "<output.zip>" --format zip --tier standard
```

如果当前版本的 `--format zip` 对单文件输出语义不同，则使用：

```bash
mineru-kit parse "<pdf>" -o "<output_dir>" --format zip --tier standard
```

具体参数以现场 `mineru-kit parse --help` 为准；适配层必须把实际命令写入日志。

### 3.3 输出解包

ZIP 中优先读取：

1. `structured_content.json`（4.1 ZIP 实际产物，按页分组的块，含页码、类型、文本/表格/公式）；
2. `content_list_v2.json` / `content_list.json`（若当前版本提供）；
3. `middle_json.json`（版面结构，作为回退）；
4. `markdown.md` / `full.md`（最终 Markdown，作为最后回退）；
5. `images/`（图片资源，保留在缓存目录供人工查看）。

---

## 4. 配置

`config.yaml` 新增：

```yaml
parser:
  mineru:
    enabled: false
    command: "mineru-kit"          # 或绝对路径，如 C:\...\.mineru\Scripts\mineru-kit.exe
    python: ""                     # 可选：用 python -m mineru... 调用
    tier: "standard"               # flash / basic / standard / advanced
    ocr_mode: "auto"               # auto / txt / ocr
    device: "auto"                 # auto / cpu / gpu
    timeout_sec: 1800
    fallback_to_pymupdf: true
    keep_artifacts: true
    max_output_mb: 2048
    min_markdown_chars: 200        # 低于此字符数视为失败
    concurrent_jobs: 1             # 默认串行，避免 CPU/GPU 争抢
    language: "en"                 # 可选，MinerU 支持时传入
```

环境变量覆盖：

- `ACDB_MINERU_COMMAND`
- `ACDB_MINERU_TIER`
- `ACDB_MINERU_TIMEOUT`
- `MINERU_MODEL_SOURCE`（MinerU 自身使用）

---

## 5. 后端适配层

新增 `app_core/parser/mineru_backend.py`：

### 5.1 `MinerUConfig`

从 `AppConfig` 读取配置，允许 UI 运行时修改。

### 5.2 `MinerUError`

区分：

- `MinerUNotInstalled`
- `MinerUTimeout`
- `MinerUModelMissing`
- `MinerUOutputInvalid`
- `MinerUUnavailable`

### 5.3 `MinerUProbe`

- `version()`：执行 `mineru version --json` 或 `mineru-kit --version`，解析版本；
- `available()`：命令存在且可执行；
- `diagnose()`：返回版本、路径、tier、模型目录、错误信息；
- `model_status()`：可选，运行 `mineru-kit models list`/`models download --dry-run`（以实际 CLI 为准）。

### 5.4 `MinerUConverter`

```python
class MinerUConverter:
    def convert(self, pdf_path: Path, *, doc_id: str, pdf_sha256: str) -> MinerUResult: ...
```

职责：

- 计算缓存 key：`pdf_sha256 + mineru_version + tier + ocr_mode + language`；
- 缓存目录：`workspace/mineru/<doc_id>/<cache_key>/`；
- 若已有 `full.md`/`content_list_v2.json` 且 `manifest.json` 标记成功，直接复用；
- 调用 `subprocess.run([...], cwd=..., timeout=..., capture_output=True, text=True, encoding="utf-8", errors="replace")`；
- 设置 `MINERU_MODEL_SOURCE`、`MINERU_HOME` 等环境变量（如果配置）；
- 不允许 shell=True；
- 输出大小上限；
- 解包 ZIP（安全解压：拒绝绝对路径、`..`、符号链接）；
- 写 `manifest.json`：版本、命令、tier、耗时、输出文件、pdf hash、状态；
- 失败时清理半成品，记录 stderr 尾部（限制长度）。

### 5.5 `MinerUResult`

```python
@dataclass
class MinerUResult:
    markdown: str
    page_markdown: list[tuple[int, str]]
    page_count: int
    content_list: list[dict]
    output_dir: Path
    version: str
    tier: str
    elapsed_sec: float
```

---

## 6. 页码与结构化恢复

### 6.1 优先 `structured_content.json` / `content_list_v2.json`

如果存在 `structured_content.json`（4.1 ZIP 实测产物）或 V2 content list：

- 每个 block 有页码（0-based 或 1-based，需探测）；
- 按页拼接 Markdown，在页边界插入内部标记：

```markdown
<!-- ACDB-PAGE: 12 -->
```

- 表格 block → Markdown 表格；
- 公式 block → `$$...$$`；
- 图片 block → 保留引用，正文中可忽略或写成 `![...](images/...)`；
- 标题/段落保持原顺序；
- 生成 `page_markdown` 供 `parse_markdown` 使用。

如果 V2 缺失，使用 V1 content_list；再缺失则使用 `full.md`，页码未知但 `page_count` 仍来自 PyMuPDF。

### 6.2 扩展 `PDFParser.parse_markdown`

新增可选参数：

```python
page_markdown: list[tuple[int, str]] | None = None
```

- 如果提供，按页处理，把页码传给 `_MarkdownBuilder.heading/paragraph/table/formula/code`；
- 识别 `<!-- ACDB-PAGE: N -->` 标记，切换当前页码；
- 保持现有 Markdown 解析行为不变；
- 表格/公式/代码块跨页时取起始页。

### 6.3 `ParsedDocument`

新增或在 metadata 中写入：

```python
metadata["markdown_backend"] = "mineru"
metadata["mineru_version"] = "4.1.7"
metadata["mineru_tier"] = "standard"
metadata["mineru_elapsed_sec"] = 123.4
metadata["mineru_output_dir"] = "..."
```

`source_path` 仍指向原 PDF；`doc_id` 不变。

---

## 7. Pipeline 接入

### 7.1 初始化

`Pipeline.__init__` 创建 `MinerUConfig` / `MinerUConverter`，并传给 `PDFParser`：

```python
parser = PDFParser(mineru=converter, mineru_fallback=True)
```

或者 `PDFParser.parse()` 内部优先尝试 MinerU；为保持测试可控，推荐在 `PDFParser` 构造时注入 `mineru_backend`。

### 7.2 `parse_document`

在 PDF 分支：

1. `PDFParser.parse(pdf_path, doc_id=...)`；
2. 若 MinerU 成功，返回 `ParsedDocument`，`markdown_backend="mineru"`；
3. 若 MinerU 失败且 `fallback_to_pymupdf=true`，记录 `WARN` 事件并调用原 PyMuPDF 解析；
4. 若 MinerU 关闭，保持现有行为；
5. 扫描件检测：
   - MinerU 成功且 Markdown 长度达到 `min_markdown_chars`：**跳过** `_text_layer_too_sparse` 的失败判定；
   - 回退 PyMuPDF 且文本层稀疏：仍失败，并提示“可安装 MinerU 4 进行 OCR”。

### 7.3 缓存

- `parse_document` 每次先查 MinerU 缓存；
- 强制重新提炼（force）时：
  - 默认复用 MinerU 缓存（PDF 内容未变）；
  - UI 可勾选“重新 OCR”或清除该文献 MinerU 缓存；
- 文献库增加“重新 OCR 选中文献”按钮（仅 MinerU 开启时可用）。

### 7.4 并发

- MinerU 调用受 `concurrent_jobs` 信号量限制，默认 1；
- 不阻塞 GUI：解析本来在 worker/QThread 中；
- 记录队列等待时间，避免多个大 PDF 同时打满 CPU/GPU。

---

## 8. UI

### 8.1 设置页新增“MinerU 4 高质量解析”卡片

字段：

- 启用 MinerU 4
- 命令 / 可执行文件路径（可浏览选择）
- Python（可选）
- 解析档位：flash / basic / standard / advanced
- OCR 模式：auto / txt / ocr
- 设备：auto / cpu / gpu
- 超时秒数
- 失败回退 PyMuPDF
- 保留中间产物
- 并发数
- 状态：版本、路径、模型状态
- 按钮：
  - **检测 MinerU**
  - **打开缓存目录**
  - **安装说明**（打开官方 Quick Start）
  - 可选：**下载当前档位模型**（调用 `mineru-kit models download`，提示耗时/磁盘）

### 8.2 文献库

- 每篇文献显示解析后端徽标：`PyMuPDF` / `MinerU 4.1.7 (standard)`；
- 解析失败时提示“可安装 MinerU 4 处理扫描件”；
- 新增“重新 OCR 选中文献”操作（仅开启 MinerU 时显示）。

### 8.3 文献详情

- 显示 `Markdown 后端`、MinerU 版本、档位、耗时；
- 显示 MinerU 缓存目录按钮；
- 如果回退到 PyMuPDF，显示警告原因。

---

## 9. 测试

### 9.1 单元测试

1. 未安装 MinerU：`available() == False`，不影响 PyMuPDF；
2. fake `mineru-kit` 脚本输出 ZIP：适配层解析成功；
3. 超时：回退 PyMuPDF，记录原因；
4. 非零退出：回退；
5. 输出 ZIP 路径穿越：拒绝；
6. 缓存命中：第二次不调用子进程；
7. 配置变化（tier/ocr_mode）使缓存失效；
8. `content_list_v2` → Markdown 页码标记正确；
9. `parse_markdown(page_markdown=...)` 页号正确；
10. MinerU 成功时扫描件不被 `_text_layer_too_sparse` 误杀；
11. 回退后仍按原逻辑判定扫描件；
12. GUI 设置保存/读取、检测按钮；
13. 文献库“重新 OCR”按钮可见性。

### 9.2 集成测试

- fake CLI + 小 PDF fixture 跑 `Pipeline.parse_document`；
- 断言 `metadata["markdown_backend"] == "mineru"`；
- 断言 sections/bundles 正常生成；
- 断言 `bundle` 页码来自 MinerU。

### 9.3 真实 MinerU 调试

- 安装 MinerU 4.x 到独立 venv；
- 用一篇现有文献 PDF 跑 `mineru-kit parse`；
- 验证输出 ZIP、Markdown、页码；
- 对比 PyMuPDF 与 MinerU 的章节数、证据包数、扫描件处理；
- 不把模型权重打进发布包。

---

## 10. 失败与兼容策略

- MinerU 安装失败/模型缺失：设置页显示明确错误，不阻塞普通解析；
- MinerU 输出为空/字符数过低：视为失败并回退；
- 旧文献 `parsed.md` 已存在：默认不自动重解析；用户点击“重新 OCR/重新解析”才覆盖；
- 用户已确认卡片不受解析影响；
- 重新解析不会自动删除旧卡；由现有重处理逻辑处理增量/强制模式；
- MinerU 缓存可清理，不影响 SQLite/知识卡。

---

## 11. 验收标准

- 设置页可启用/检测 MinerU 4 并保存配置；
- 扫描 PDF 在 MinerU 成功时能生成 Markdown 和证据包，不再直接失败；
- 有文本层的 PDF 可以用 MinerU 重新解析，章节/页码/表格/公式保留；
- MinerU 失败时自动回退 PyMuPDF，并给出可读原因；
- 同一 PDF 不重复 OCR；
- 解析后端、版本、档位、耗时可在文献详情查看；
- 不把 MinerU/PyTorch/模型打包进主程序；
- 全量测试通过，生成 V2.5.0 Patch/Full 发布包。

---

## 12. 一句话

MinerU 4 作为“可选的高质量 OCR/Markdown 预处理器”接入现有解析链路：能用则用、失败回退、结果缓存、页码保留、后端可追溯，主程序仍然轻量。
