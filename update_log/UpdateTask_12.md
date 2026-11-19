# UpdateTask_12.md — `AtomicCalculationDB` (V3.1.0) 元数据智能识别与高精校准、清单行内防误触锁定与专属元数据编辑对话框

> **版本**：`v3.1.0`  
> **构建号**：`20261119`  
> **核心里程碑**：
> 1. **基于 MinerU Markdown 与 OCR 的文献元数据高精智能识别与校准机制**：
>    - 彻底根除因各大出版商封面下载水印（如 IOPscience, ScienceDirect, Springer, IEEE Xplore）导致的误判问题（例如将下载日期 2015 误识别为发表年份、将 IOPscience 水印误判为 `science` 期刊）；
>    - 构建 `clean_publisher_banners` 过滤器，剥离下载 IP、下载日期及免责声明；
>    - 构建 `extract_metadata_from_markdown` 与 `extract_metadata_for_document`，解析物理引文规则（如 `1982 J. Phys. B: At. Mol. Phys. 15 1319`）、标题多级标题行、纯净作者列表及 DOI；在联网环境下通过 Crossref/OpenAlex 权威校验并清洗 MathML 标签，离线环境下高精度本地正则提取；
>    - 提供 `MetadataDiffDialog` 对比确认对话框，直观呈现当前值与建议值差异，支持逐项勾选与一键校准写回数据库。
> 2. **文献清单禁止行内随意编辑与 DOC-ID 编辑态 Bug 根治**：
>    - 彻底修复关闭文献详情工作台后，DOC-ID 单元格意外进入行内文本框编辑状态的严重缺陷；
>    - 文献清单表格显式锁定 `setEditTriggers(QAbstractItemView.NoEditTriggers)`，全量单元格移除 `Qt.ItemIsEditable`；双击任何列统一直接打开详情工作台；
>    - 严格确立 DOC-ID 全局权威唯一且只读原则，禁止任何界面手动随意篡改。
> 3. **文献详情工作台专属元数据编辑表单 (`DocumentEditDialog`)**：
>    - 在文献详情工作台“文献信息与解析状态”卡片上方新增专属操作栏：
>      - `✏️ 编辑文献信息` 按键：弹出规范化独立表单，支持手动修改文献标题、文献类型（PAP 论文 / MAN 手册 / BOK 专著 / REV 综述）、出版年份、期刊/出版社、作者列表、DOI、arXiv ID；
>      - `🔍 智能校准元数据 (MinerU/OCR)` 按键：一键启动 OCR/Markdown 识别核对；
>    - 文献库右键菜单同步集成单篇与批量智能校准、单篇手动编辑快捷入口。

---

## 1. 元数据智能识别与校准引擎 (`app_core/parser/metadata_extractor.py`)

### 1.1 出版商下载水印脱水过滤器 (`clean_publisher_banners`)
- **针对痛点**：PDF 转换文本前置常带有 IOP, Elsevier, Springer, IEEE 的下载封面，含 `This content has been downloaded from IOPscience... Download details: IP Address: 142.103... 06/09/2015 at 21:58`。旧算法会将 `2015` 误作为论文年份，将 `IOPscience` 末尾匹配为 `Science` 期刊。
- **过滤机制**：扫描前 100 行，剔除所有下载日期、IP 地址、版权声明与下载平台水印，保留真实论文文本层。

### 1.2 高精 Markdown 引文与元数据抽取 (`extract_metadata_from_markdown`)
- **DOI 与权威库联动**：提取标准 `10.xxxx/...` 及 IOP 特殊 URL 格式（`0022-3700/...` -> `10.1088/...`）。联网环境下调用 Crossref/OpenAlex API 获取官方权威著录信息，并剔除 MathML/HTML 标记；
- **物理期刊引文正则扫描**：
  - 格式一：`1982 J. Phys. B: At. Mol. Phys. 15 1319` -> 年份 `1982`，期刊 `J. Phys. B: At. Mol. Phys.`；
  - 格式二：`PHYSICAL REVIEW A 79 , 022511 2009` -> 年份 `2009`，期刊 `Physical Review A`；
  - 格式三：`J. Phys. B: At. Mol. Phys. 15 (1982) 1319-1331`；
- **标题多级合并**：扫描前 25 行 Markdown 标题（`# `, `## `），自动跳过引文行与章节编号（如 `# 1. Introduction`），并合并跨行长标题；
- **作者清洗过滤**：定位标题后方至摘要前的作者行，自动剔除大学、研究所、系所、街道等地址信息，剥离脚注符号（`?`, `+`, `§`, `†`, `‡` 等），规整为标准作者列表。

### 1.3 统一文献提取接口 (`extract_metadata_for_document`)
- 自动检测并优先读取 `storage/<doc_id>/parsed.md`（MinerU 结构化解析成果）；
- 若无 Markdown 则回退到本地 PDF 布局分析与 Zotero 标准文件名提取；
- 为单篇校准、批量校准与修复脚本提供一致的高精度数据源。

---

## 2. 界面交互与防误触重构 (`app_core/ui/views/library.py`)

### 2.1 彻底禁止文献清单行内直接编辑
- **Bug 根因**：原 `library.py` 中 `self.table.setEditTriggers(DoubleClicked | EditKeyPressed)`，当用户双击第 0 列打开详情弹窗后，Qt 事件循环在关闭弹窗瞬间再次分发鼠标双击至底层单元格，导致 DOC-ID 进入内联编辑状态。
- **全面锁定**：
  1. `self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)`；
  2. `_populate_table` 中对所有列（包括 DOC-ID、标题、年份、期刊、作者等）统一声明 `item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)`，完全清除 `Qt.ItemIsEditable`；
  3. `_cell_double_clicked` 统一映射为 `self._open_row(row, column)`，双击任何单元格均直接打开文献详情工作台。

### 2.2 右键菜单功能扩展
- 新增 `🔍 智能校准元数据 (MinerU/OCR)`；
- 新增 `✏️ 编辑文献信息`；
- 多选文献时新增 `🔍 批量智能校准元数据 (N 篇)`。

---

## 3. 专属元数据操作对话框 (`app_core/ui/dialogs.py`)

### 3.1 手动元数据编辑表单 (`DocumentEditDialog`)
- **只读 DOC-ID**：显示为禁用/只读输入框，附带系统权威只读提示；
- **规整字段输入**：
  - 文献标题（支持多行折行编辑，至少 5 字符）；
  - 文献类型（下拉框：PAP 论文 / MAN 手册 / BOK 专著 / REV 综述）；
  - 出版年份（纯 4 位数字校验，1800 ~ 2200）；
  - 期刊 / 出版社；
  - 作者列表（多行支持逗号、分号或换行分隔输入）；
  - DOI 与 arXiv ID。
- **保存与数据库同步**：提交后调用 `db.upsert_document`，刷新详情页与文献清单。

### 3.2 智能校准元数据比对窗 (`MetadataDiffDialog`)
- **四列比对矩阵**：`[应用勾选, 字段名称, 当前数据库值, 智能识别建议值]`；
- **智能预选**：对于识别出有效值且与当前数据库值不一致的字段，默认自动勾选并以绿色高亮显示；
- **辅助操作**：提供“全选有变更项”、“全部取消勾选”，用户可精准把控修改内容；
- **写回机制**：仅更新用户确认勾选的字段，避免覆盖用户已确认的个性化设定。

### 3.3 文献详情工作台操作栏与无缝刷新 (`DocumentDetailDialog`)
- 卡片上方呈现专用操作栏（`🔍 智能校准元数据` 与 `✏️ 编辑文献信息`）；
- 修改后实时刷新顶部类型 Chip、标题 Label、副标题（作者 · 期刊 · 年份）以及下方的元数据网格卡片；
- 通过 `data_changed = Signal()` 联动父级主窗口与文献清单自动更新。

---

## 4. 自动化测试与质量验收 (`tests/test_v310.py`)

- **全量测试通过**：245 项自动化单元测试全部一次性通过（0 失败，0 错误）；
- **专项测试覆盖**：
  1. `test_v310_version_json`：校验版本号为 3.1.0，构建号 20261119；
  2. `test_clean_publisher_banners`：校验出版商水印与下载信息完全剥离；
  3. `test_extract_metadata_from_markdown_iop_pyper1982`：验证真实 MinerU 样本文献准确提取 1982 年、J. Phys. B 期刊、3 位真实作者及 DOI，彻底排除 2015 与 science 误判；
  4. `test_extract_metadata_from_markdown_pra`：验证 Physical Review A 引文及 DOI 提取；
  5. `test_extract_metadata_for_document`：验证从 `storage/<doc_id>/parsed.md` 读取并精准解析；
  6. `test_document_edit_dialog`：验证表单修改、DOC-ID 只读锁定及数据库写回；
  7. `test_metadata_diff_dialog`：验证比对差异预选逻辑与更新字典提取；
  8. `test_library_view_table_not_editable`：验证表格 `NoEditTriggers` 及所有单元格无 `ItemIsEditable` 标志。
