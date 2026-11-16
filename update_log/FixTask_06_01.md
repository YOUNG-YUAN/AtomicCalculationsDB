# FixTask_06_01.md — `AtomicCalculationDB` (V2.5.0) MinerU 4 OCR/Markdown 解析接入

## 1. 实现内容

### 1.1 MinerU 4 后端

新增 `app_core/parser/mineru_backend.py`：

- 自动检测 `mineru-kit` 可执行文件与版本；
- 调用无状态 CLI：
  ```bash
  mineru-kit parse <pdf> -o <cache>/result.zip --format zip \
    --tier <tier> --ocr-mode <mode> --pages all
  ```
- 支持 `flash / basic / standard / advanced`、`auto/txt/ocr`、设备、超时、并发信号量；
- ZIP 安全解包（拒绝绝对路径与 `..` 越界）；
- 优先读取 `structured_content.json`，回退 `middle_json.json`、`markdown.md`；
- 从页面块恢复页码、标题、段落、表格、公式和图片引用；
- 缓存 key = PDF SHA256 + MinerU 版本 + tier + OCR 模式 + language + command；
- 缓存目录：`workspace/mineru/<doc_id>/<cache_key>/`；
- 输出 `full.md`、`page_markdown.json`、`manifest.json`；
- 失败写 manifest 错误并清理半成品；下一次自动重试。

### 1.2 PDFParser / Pipeline

- `PDFParser` 新增 `mineru_backend`、`mineru_fallback`；
- MinerU 成功：直接返回 `ParsedDocument`，`metadata["markdown_backend"]="mineru"`；
- MinerU 失败：记录 `last_mineru_error`，自动回退 PyMuPDF；
- `parse_markdown()` 支持 `page_markdown` 和 `<!-- ACDB-PAGE: N -->` 页标记；
- `_text_layer_too_sparse()` 对 MinerU 成功结果跳过扫描件误判；
- `Pipeline` 自动创建 `MinerUConverter`；
- 文献 `metadata_json` 持久化：
  - `markdown_backend`
  - `mineru_version`
  - `mineru_tier`
  - `mineru_elapsed_sec`
  - `mineru_output_dir`
  - `mineru_fallback_error`
- 文献详情显示 Markdown 后端。

### 1.3 设置页

新增“MinerU 4 高质量 OCR / Markdown”卡片：

- 启用开关
- 命令/可执行文件路径 + 浏览
- 解析档位
- OCR 模式
- 设备
- 超时
- 失败回退
- 保留中间产物
- 检测 MinerU 版本
- 打开 MinerU 缓存
- 官方安装说明链接

配置写入 `parser.mineru.*`，不把 MinerU/PyTorch/模型打进主安装包。

## 2. 实机验证

环境：

- MinerU **4.1.7**
- 独立 venv：`.mineru`（Python 3.12.11）
- Windows 11 / CPU

测试：

1. `mineru version --json` 返回 4.1.7。
2. 4 页论文 `zou2002.pdf`：
   - `flash + txt`：3.2s，17,986 字符，4 页。
3. 扫描版 12 页论文：
   - `flash + ocr`：265.6s，60,978 字符，12 页。
4. `Pipeline.parse_document` 真实小论文：
   - Markdown 后端：`mineru`
   - 版本：4.1.7
   - 4 页、2 个小节、16 个证据包
   - 文献 metadata 正确持久化。
5. 失败回退、ZIP 越界、缓存命中、页码标记均有单元测试。

## 3. 测试

- 新增 `tests/test_v250.py`：
  - 页码标记；
  - MinerU 失败回退 PyMuPDF；
  - 扫描件稀疏检测跳过；
  - structured_content 适配；
  - ZIP 路径穿越拒绝；
  - 缓存命中只调用一次 CLI；
  - 配置默认值与 `parse_markdown(page_markdown=...)`。
- 设置页新增 GUI 断言。
- 全量回归：**139 passed**。

## 4. 构建与部署

- 版本：**V2.5.0**
- 发布包：
  - `release/AtomicCalculationDB_Patch_v2.5.0.zip`（447,094 B，88 个文件）
    - SHA256 `6781b3ea9b161c50278dcd547599eebc4d81b035799ff770c983bf4fef586fa1`
  - `release/AtomicCalculationDB_Full_v2.5.0.zip`（1,714,475,925 B，9,953 个文件）
    - SHA256 `aaa7476102c15b81462d6e6964ef952c14da10a341e90554aacb124d9302035f`
- 两个包均通过 `unzip -t`。
- 安装目录仍为 V2.3.11，应用未运行；本次未覆盖安装。

## 5. 使用说明

1. 安装 MinerU 4 到独立环境：
   ```bash
   uv venv .mineru --python 3.12
   .mineru\Scripts\activate
   uv pip install -U "mineru>=4.0,<5"
   ```
2. 打开设置 → MinerU 4 卡片，命令选择：
   ```text
   <项目>/.mineru/Scripts/mineru-kit.exe
   ```
3. 选择档位/OCR 模式并保存。
4. 对已有文献执行“强制重提”即可重新走 MinerU；后续新文献自动使用。
5. 首次使用某档位可能下载模型；缓存位于 `workspace/mineru/`。
