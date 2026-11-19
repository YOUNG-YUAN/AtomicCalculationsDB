# UpdateTask_12.md — `AtomicCalculationDB` (V3.1.0) BibTeX文献信息导入更新、清单行内防误触锁定、专属元数据编辑对话框与处理模型智能记忆

> **版本**：`v3.1.0`  
> **构建号**：`20261119`  
> **核心里程碑**：
> 1. **导入 BibTeX 自动更新文献信息机制 (`BibtexImportDialog` & `parse_bibtex_text`)**：
>    - 鉴于纯 OCR / MinerU 文本启发式推断在复杂论文版面上易将单位机构、大学地点等误识别为人名或混乱元数据，彻底回归物理学界业界标准：**BibTeX 格式直接导入**；
>    - 移除 MinerU 启发式元数据推断与出版商水印正则清洗逻辑，新增轻量级高兼容性的零依赖 BibTeX 解析引擎；
>    - 支持 `@article`, `@book`, `@manual`, `@misc`, `@inproceedings` 等常见类型；自动解析并转换花括号与引号字符串、标准作者规整（`Last, First` -> `First Last`，`and` 联合作者）、年份、期刊、出版社、DOI 与 arXiv ID；
>    - 提供 `BibtexImportDialog`：既支持直接选择本地 `.bib` / `.txt` 文件导入，也支持直接粘贴从 Google Scholar、APS Physical Review、IOP、arXiv 等一键复制的 BibTeX 文本片段；
>    - 配合 `MetadataDiffDialog` 比对差异窗口，直观呈现当前库中值与 BibTeX 建议值差异，用户自主勾选并一键更新回数据库。
> 2. **文献清单禁止行内随意编辑与 DOC-ID 编辑态 Bug 彻底根治**：
>    - 彻底修复关闭文献详情工作台后，底层表格对应行的 DOC-ID 单元格意外进入内联文本输入编辑框的严重缺陷；
>    - 文献清单表格显式声明 `setEditTriggers(QAbstractItemView.NoEditTriggers)`，全部单元格 item 彻底移除 `Qt.ItemIsEditable`；
>    - 双击任何列统一直接打开详情工作台；严格确立 DOC-ID 唯一且全局只读原则，禁止任何界面手动直接篡改。
> 3. **文献详情工作台专属元数据编辑表单 (`DocumentEditDialog`)**：
>    - 在文献详情工作台“文献信息与解析状态”卡片上方新增专属操作栏：
>      - `✏️ 编辑文献信息` 按键：弹出规范化独立编辑表单，支持手动修改文献标题、文献类型（PAP 论文 / MAN 手册 / BOK 专著 / REV 综述）、出版年份、期刊/出版社、作者列表、DOI、arXiv ID；
>      - `📖 导入BibTex自动更新文献信息` 按键：一键唤起 BibTeX 导入与差异核对窗口；
>    - 文献库右键菜单同步集成单篇与批量导入 BibTeX、单篇手动编辑快捷入口。
> 4. **网页 AI 半自动提炼“处理模型”智能记忆与初始模型升级 (`GPT-6.2 Sol Ultra`)**：
>    - 全新程序/工作区的默认提炼模型升级为最新的旗舰大模型：`GPT-6.2 Sol Ultra`；
>    - 建立跨会话模型记忆机制：用户在半自动提炼工作台（顶栏处理模型下拉框、粘贴回复弹窗等）每次更改或自定义输入模型后，自动持久化至工作区配置（`config.yaml` 中 `semi_auto.agent_model`）以及系统配置（`QSettings`）；
>    - 下次打开工作台或生成新任务包时，自动以最后一次输入的模型作为默认配置；自定义模型自动置顶加入快捷下拉列表，免去反复重新输入的繁琐操作。

---

## 1. BibTeX 解析与元数据映射引擎 (`app_core/parser/metadata_extractor.py`)

### 1.1 纯 Python 轻量零依赖 BibTeX 解析器
- **`parse_bibtex_text(text: str) -> list[dict[str, Any]]`**：
  - 自动定位 `@type{key, ...}` 条目，精确处理字段内的多行文本、单引号、双引号与嵌套花括号；
  - 提取字段并自动小写化键名（`title`, `author`, `journal`, `publisher`, `year`, `doi`, `eprint`/`arxivId`, `volume`, `pages` 等）；
  - 作者字段自动按照 `and` 分割，并将 BibTeX 规范的 `Last, First` 倒置为人性化的 `First Last`；清洗多余的保护大括号 `{}`。
- **`parse_bibtex_file(file_path: str | Path) -> list[dict[str, Any]]`**：
  - 支持 UTF-8 与本地编码自动读取 `.bib`、`.bibtex` 或 `.txt` 文本文件并转为标准字典列表。
- **`bibtex_to_metadata(entry: dict[str, Any]) -> DocumentMetadata`**：
  - 将 BibTeX 条目精准映射为 `AtomicCalculationDB` 核心的 `DocumentMetadata` 结构；
  - 智能类型映射：`@article` -> `PAP` (论文)、`@manual` -> `MAN` (手册)、`@book` / `@incollection` -> `BOK` (专著) 等。

---

## 2. 界面交互与防误触重构 (`app_core/ui/views/library.py`)

### 2.1 彻底禁止文献清单行内直接编辑
- **Bug 根因**：原 `library.py` 中 `self.table.setEditTriggers(DoubleClicked | EditKeyPressed)`，当用户双击第 0 列打开详情弹窗后，Qt 事件循环在关闭弹窗瞬间再次分发鼠标双击至底层单元格，导致 DOC-ID 进入内联编辑状态。
- **全面锁定**：
  1. `self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)`；
  2. `_populate_table` 中对所有列（包括 DOC-ID、标题、年份、期刊、作者等）统一声明 `item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)`，完全清除 `Qt.ItemIsEditable`；
  3. `_cell_double_clicked` 统一映射为 `self._open_row(row, column)`，双击任何单元格均直接打开文献详情工作台。

### 2.2 右键菜单功能扩展
- 新增 `📖 导入BibTex自动更新文献信息`；
- 新增 `✏️ 编辑文献信息`。

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

### 3.2 BibTeX 导入与差异核验对话框 (`BibtexImportDialog` & `MetadataDiffDialog`)
- **双模输入**：既可以通过文件选择器选择外部 `.bib` 文件，也支持在文本区域直接粘贴从网页端（如 Google Scholar、物理期刊官网）复制的 BibTeX 代码块；
- **比对确认矩阵**：解析出的元数据送入 `MetadataDiffDialog`，直观呈现当前库中值与 BibTeX 解析值差异；
- **智能预选与写回**：对存在有效差异的字段默认自动勾选，用户可自由选择需要应用的字段（如保留自己设定的文献类型，仅更新标题、作者和期刊），点击确认后一键写回数据库。

### 3.3 文献详情工作台操作栏与无缝刷新 (`DocumentDetailDialog`)
- 卡片上方呈现专用操作栏（`📖 导入BibTex自动更新文献信息` 与 `✏️ 编辑文献信息`）；
- 修改后实时刷新顶部类型 Chip、标题 Label、副标题（作者 · 期刊 · 年份）以及下方的元数据网格卡片；
- 通过 `data_changed = Signal()` 联动父级主窗口与文献清单自动更新。

---

## 4. 处理模型智能记忆机制 (`app_core/ui/semi_auto_dialog.py` & `app_core/models/semi_auto.py`)

### 4.1 初始旗舰默认模型升级
- `DEFAULT_AGENT_MODEL` 统一升级为 `GPT-6.2 Sol Ultra`；
- `app_core/config.py` 的 `DEFAULTS` 中正式增加 `"semi_auto": {"agent_model": "GPT-6.2 Sol Ultra", "max_batch_tokens": 20000}`。

### 4.2 动态记忆与持久化
- `save_default_agent_model(model_name, config)`：
  - 同步写入 `config.set("semi_auto.agent_model", model_name)` 并持久化；
  - 同步更新系统级注册表/首选项 `QSettings("AtomicCalculationDB", "AtomicCalculationDB")`；
- `get_default_agent_model(config)`：优先读取配置，缺失时自动安全回退至系统首选项或 `GPT-6.2 Sol Ultra`；
- 用户在工作台下拉框手动修改（含输入完成 `editingFinished`）、点击生成提问包、导入回复或在粘贴回复弹窗选择模型时，自动触发生效并记忆。

---

## 5. 自动化测试与质量验收 (`tests/test_v310.py`)

- **全量测试通过**：246 项自动化单元测试全部一次性通过（0 失败，0 错误）；
- **专项测试覆盖**：
  1. `test_v310_version_json`：校验版本号为 3.1.0，构建号 20261119；
  2. `test_parse_bibtex_text`：验证 `@article`, `@book`, `@manual` 各种花括号、引号及作者倒置转换解析；
  3. `test_parse_bibtex_file`：验证从本地 `.bib` 文件读取并解析多条记录；
  4. `test_bibtex_to_metadata`：验证 BibTeX 条目转换到 `DocumentMetadata` 及其类型推断；
  5. `test_bibtex_import_dialog`：验证 BibTeX 导入对话框粘贴与文件读取的交互逻辑；
  6. `test_document_edit_dialog`：验证表单手动修改、DOC-ID 只读锁定及数据库写回；
  7. `test_metadata_diff_dialog`：验证 BibTeX 比对差异预选逻辑与更新字典提取；
  8. `test_library_view_table_not_editable`：验证表格 `NoEditTriggers` 及所有单元格无 `ItemIsEditable` 标志；
  9. `test_agent_model_last_used_persistence`：验证新程序默认 GPT-6.2 Sol Ultra、修改后自动持久化到 config/QSettings 以及对话框无缝记忆加载。
