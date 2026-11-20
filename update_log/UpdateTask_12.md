# UpdateTask_12.md — `AtomicCalculationDB` (V3.1.0) BibTeX文献信息导入更新、清单行内防误触锁定、专属元数据编辑对话框、处理模型智能记忆与超长数据表提问包重构 (Table-on-Demand)

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
> 5. **超长数据表文献提问包爆炸根治 (Table-on-Demand 骨架浓缩、伪结论行过滤与提问包三段式重构)**：
>    - **问题定位**：针对 ADNDT 等含上百页巨型数据表格的文献（如 `DOC-PAP-0000-E284F0`），表格跨页标题（`Table 3 (continued)`）与说明小节被原正则误判为 400+ 伪结论，加之 150 多个表格片段被重复完整打包，直接切出 100+ 个提问包且单包撑爆 22,000 Token；
>    - **雷达伪结论精准过滤**：跳过 `Explanation of Tables`、`References` 等非物理讨论附录章节；过滤 OCR `bbox` 元数据行与 bare 跨页续表标头；纯表格引用行必须为长度 >= 20 且含 >= 4 词的实质性论述句；
>    - **按需调用 (Table-on-Demand)**：语义分析提问条目是否引用表格。未引用表格的条目（如 Hamilton 矩阵元、组态波函数生成说明）携带 0 表格 Token；引用表格的条目从文档注册表中按需索引对应表格；
>    - **表格骨架浓缩 (`skeletonize_table_markdown`)**：超长表格自动浓缩为“表头 + 分割线 + 前 5 行典型数值 + 省略 N 行提示 + 表注”，既保留物理字段定义又杜绝 Token 膨胀；
>    - **提问包三段式重构**：提问包提示词重构为清晰的三大独立分区：
>      1. `【任务与输出规范】`（统一放置角色指令、任务背景、Schema 规范与 LaTeX 双反斜杠约束）；
>      2. `【具体提问条目】`（逐条清晰呈现条目上下文、回溯定义与焦点窗口，正文不再掺杂杂乱长表）；
>      3. `【参考表格附件 (按需调用)】`（仅当包内条目需要表格时在文末独立附带骨架浓缩表，跨条目去重共享）；
>    - **实测验证 (`DOC-PAP-0000-E284F0`)**：雷达命中数从 479 降至 33，证据包从 175 缩减至 17 个高价值物理条目（50 KB），在 22,000 Token 限额下单文献提问包从 100+ 暴降至 **2 个高质量提问包**！
> 6. **自主 AI Agent (Codex/Claude Code/Antigravity) 专属任务包与 CLI 自动化工作流**：
>    - **解耦自解释目录**：生成专属 `workspace/agent_tasks/<DOC-ID>/`，包含 `task_instructions.md`（带物理规则与防坑指南）、`items.json`（按需关联独立表格与图片）、`schema.json`（严格结果结构）、`parsed.md`（全文公式文本）以及 `tables/Table_X.md`；
>    - **Agent CLI 交互引擎**：支持 `python -m app_core agent export <DOC-ID>` 导出目录、`status <DOC-ID>` 查询任务状态、`submit <DOC-ID> [result.json]` 校验并自动批量入库 L2 知识卡片、关键词并推进状态至 `LLM_EXTRACTED`；
>    - **GUI 桥接与 MCP 工具联动**：GUI 运行时通过 HTTP 桥无冲突操作（避免 Qdrant/SQLite 锁），GUI 未启动时以 Headless 独立服务执行；同步在 `KnowledgeMCPServer` 中暴露对应 MCP 工具。
> 7. **本地 Agent / CLI 专属图形工作台 (`AgentTaskDialog`)、执行策略专用选项与数据表格总结入库 (DATA Cards)**：
>    - **专属执行策略**：在文献详情工作台“⚙️ 执行策略: ③ AI 快速提取”下拉框中新增第三项专属执行策略：`💻 本地 Agent / CLI 任务包`；
>    - **本地 Agent 工作台 (`AgentTaskDialog`)**：点击第 ③ 步执行按键直接唤起全新的专属工作台，呈现任务包绝对目录（复制路径/打开文件夹/重新生成）、CLI 执行命令行、Agent 通用任务提示词与原始文件列表；
>    - **条目与表格双重回复导入 (双按键)**：针对物理条目 (`items.json`) 与数据表格 (`tables/`) 分别提供 `📥 导入回复文件` 与 `📝 粘贴回复文本` 两个专属导入按键；
>    - **数据表格总结直接入库为 DATA 卡片**：支持一键生成并复制表格总结专用 Prompt，AI 根据表头、列标签及大致数值输出的总结，自动解析并转换为规范的【数据类型知识卡 (DATA Cards)】直接入库。

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

## 5. 超长数据表文献提问包重构与按需调用 (Table-on-Demand)

### 5.1 伪结论与附录章节过滤 (`app_core/parser/four_channel_radar.py`)
- **附录章节跳过**：`FourChannelRadar.scan` 针对非手册文献，自动跳过 `Explanation of Tables`、`References`、`Bibliography`、`Contents`、`Acknowledgments` 等非实质性物理讨论小节，杜绝表格说明文本与参考文献括号引发的虚假雷达命中；
- **OCR 噪点与续表过滤**：过滤包含 `{'bbox'` 的 OCR 边界框数据；过滤匹配 `^(Table|Figure)\s*\d+.*(cont.*)?$` 的纯跨页标头；
- **表格引用句实质性要求**：若句子仅匹配 `Table` 关键词而缺乏认知/对比谓词（`show`, `indicate`, `better`, `agree` 等），必须满足长度 >= 20 且词数 >= 4，彻底剔除孤立续表行。

### 5.2 按需调用与表格骨架浓缩 (`app_core/parser/table_compressor.py` & `app_core/parser/context_bundler.py`)
- **表格文档注册表**：`ContextBundler.build` 扫描全篇所有小节的 `TableBlock`，以规范化表号（`Table 1`, `Table 2` ...）构建全局索引映射；
- **语义按需匹配**：遍历条目焦点段落与焦点句，通过正则解析其引用的表号：
  - 未调用表格的条目：`table_markdown = ""`，0 表格 Token 冗余；
  - 调用表格的条目：从注册表查找对应表格并施加骨架浓缩；
- **表格骨架浓缩 (`skeletonize_table_markdown`)**：
  - 对超过 7 行的巨型数值表，保留表头、前 5 行典型数据，中间插入 `| ... (此处省略 N 行常规数值，保留表头与注释供物理模型与精度分析) |`，完整保留文末表注与物理单位定义；单张数百行的表格 Token 消耗从上万压缩至数百。

### 5.3 提问包三段式重构 (`app_core/models/web_ai_bridge.py`)
- **分区 1【任务与输出规范】**：统一置于提示词最顶部，包含系统提示词、全局前提、任务目标、严格 JSON 输出格式要求以及 LaTeX 双反斜杠（`\\frac`, `\\beta`）转义规则；
- **分区 2【具体提问条目】**：逐项呈现各 ITEM 的所属文献、焦点窗口、回溯定义与检测通道；条目正文不再被庞大长表切断，若该条目调用了表格，标注 `## 表格调用说明:（详见文末【参考表格附件】[表格附件 N]）`；
- **分区 3【参考表格附件 (按需调用)】**：仅当提问包内至少有一个条目调用表格时才在文末追加；跨条目自动去重共享，避免同一张表重复出现。

---

## 6. 自动化测试与质量验收 (`tests/test_v310.py`)

- **全量测试通过**：249 项自动化单元测试全部一次性通过（0 失败，0 错误）；
- **专项测试覆盖**：
  1. `test_v310_version_json`：校验版本号为 3.1.0，构建号 20261119；
  2. `test_parse_bibtex_text`：验证 `@article`, `@book`, `@manual` 各种花括号、引号及作者倒置转换解析；
  3. `test_parse_bibtex_file`：验证从本地 `.bib` 文件读取并解析多条记录；
  4. `test_bibtex_to_metadata`：验证 BibTeX 条目转换到 `DocumentMetadata` 及其类型推断；
  5. `test_bibtex_import_dialog`：验证 BibTeX 导入对话框粘贴与文件读取的交互逻辑；
  6. `test_document_edit_dialog`：验证表单手动修改、DOC-ID 只读锁定及数据库写回；
  7. `test_metadata_diff_dialog`：验证 BibTeX 比对差异预选逻辑与更新字典提取；
  8. `test_library_view_table_not_editable`：验证表格 `NoEditTriggers` 及所有单元格无 `ItemIsEditable` 标志；
  9. `test_agent_model_last_used_persistence`：验证新程序默认 GPT-6.2 Sol Ultra、修改后自动持久化到 config/QSettings 以及对话框无缝记忆加载；
  10. `test_skeletonize_table_markdown`：验证短表格原样保留、长表格骨架浓缩并注入省略行提示；
  11. `test_table_on_demand_and_pseudo_conclusion_filtering`：验证跳过附录章节与续表标头、按需为引用条目装配骨架表、未引用条目携带 0 表格 Token；
  12. `test_restructured_question_packets`：验证提问包顶部【任务与输出规范】、中部【具体提问条目】与底部【参考表格附件 (按需调用)】的三段式布局。

---

## 7. 真实超长表格文献实测验证 (`DOC-PAP-0000-E284F0`)

- **测试对象**：*Relativistic CI calculations of spectroscopic data for the $2p^6$ and $2p^5 3l$ configurations in Ne-like ions between Mg III and Kr XXVII* (P. Jönsson et al., *ADNDT* 100, 1–108 (2014))；
- **文献特征**：108 页长篇巨著，正文理论与数据讨论约 4 页，后续为 `Explanation of Tables` 与 100 余页密集能级/跃迁率数值大表；
- **优化前后实测指标对比**：
  | 指标项 | 优化前状态 | 优化后状态 (v3.1.0) | 提升效果 |
  | :--- | :--- | :--- | :--- |
  | **雷达命中数 (Radar Hits)** | 479 次 (439 处为续表标头与说明) | **33 次** (全为核心物理讨论句) | 降噪 **93.1%** |
  | **证据包数量 (ContextBundles)** | 175 个 | **17 个** (高价值物理条目) | 精简 **90.3%** |
  | **证据包磁盘体积 (bundles.json)** | 15,122,338 字节 (~15 MB) | **约 50 KB** | 体积压缩 **99.6%** |
  | **表格按需调用 (Table-on-Demand)** | 全部 175 包均堆满原始巨型表格 | **13 条目 0 表格，4 条目按需附骨架表** | 无关表格零冗余 |
  | **提问包总数 (22,000 Token 限额)** | **100+ 个提问包** | **仅 2 个提问包** | **提问包压缩 98%！** |
  | **提问包 #1** | 严重溢出/切片爆炸 | 14 项条目，预估 20,898 Token | 充分利用单包上下文 |
  | **提问包 #2** | 严重溢出/切片爆炸 | 3 项条目，预估 3,123 Token | 轻量快速导入 |

---

## 8. 自主 AI Agent (Codex / Claude Code / Antigravity) 专属自解释任务包与 CLI 自动化工作流

针对 autonomous AI agents（如 Codex、Claude Code、Antigravity 等开发/研究智能体），彻底告别网页端人工分包复制粘贴，构建基于文件系统与命令行驱动的**自解释任务包目录结构**与 **CLI 自动化闭环**：

### 8.1 任务包解耦与目录规范 (`workspace/agent_tasks/<DOC-ID>/`)
Agent 接收到文献任务时，系统不再需要向上下文直接灌入上百 KB 原始文本，而是生成一个完全解耦、自解释的专属任务工作区目录：
```text
workspace/agent_tasks/<DOC-ID>/
├── task_instructions.md   # 自解释任务指引：物理提取规则、目录使用指南、LaTeX 转义规范
├── items.json             # 核心物理提炼条目（包含焦点句、上下文窗口、回溯定义、关联表格文件与图片）
├── schema.json            # 提交结果严格 JSON Schema（对应系统 ExtractionResult）
├── parsed.md              # 全篇高质量 Markdown 原文（包含完整公式与文本，供 Agent 全文查阅）
├── tables/                # 骨架脱水后的独立表格 Markdown
│   ├── Table_1.md
│   ├── Table_2.md
│   └── ...
└── images/                # 关联图表切片图片（若存在）
    └── ...
```

### 8.2 核心模块实现与职责划分
1. **`AgentTaskPackager` (`app_core/sync_mcp/agent_task_packager.py`)**：
   - `export_task_bundle(doc_id, out_dir=None, force=False)`：自动解析文献、提取并生成 `Table_X.md` 独立表格、复制图像、构建结构化 `items.json` 与 `schema.json`，并撰写包含物理规范与限额弹性指南的 `task_instructions.md`；
   - `submit_task_result(doc_id, result_path_or_json, agent_name, agent_model, complete=None)`：解析并严格校验 Agent 输出，支持增量提交与断点更新（部分提交标记为 `PARTIAL_EXTRACTED`，全量完成跃迁至 `LLM_EXTRACTED`）；
   - `get_task_status(doc_id)`：快速检测文献任务目录是否存在、总条目数、已完成项、进度百分比、是否已生成 `result.json` 以及库中已入库卡片统计；
   - `execute_task_bundle(doc_id, poll_interval_sec=300, ...)`：内置韧性自动执行器，遇到 429、TPM/RPM 限额或超时异常时，自动安全挂起暂停，每隔 5~10 分钟定时轮询探测，配额恢复后自动从断点继续执行，绝不中断。
2. **`AgentWorkbenchService` & `RuntimeBridgeServer` 集成**：
   - 在工作台接口与运行桥（HTTP Bridge）中增加 `export_agent_task_bundle`、`submit_agent_task_result`、`get_agent_task_status` 与 `execute_agent_task` 支持；
   - 当桌面 GUI 运行时，CLI 自动连接 HTTP Bridge 代理操作，完全避免 SQLite 和 Qdrant 向量库的文件锁与单写者冲突；若桥接不可用，自动安全回退至本地 Headless 独立服务模式。
3. **`KnowledgeMCPServer` 工具扩展 (`app_core/sync_mcp/mcp_server.py`)**：
   - 向 Claude Desktop、Cursor、Cherry Studio 等 MCP 客户端原生暴露上述四项任务工单工具。

### 8.3 限额、超时与断点续传弹性机制 (Rate Limit & Resume Resilience)
针对 CODEX、Claude、Antigravity 等自主 Agent 在批量处理大量条目时存在的配额与速率限制（HTTP 429、TPM/RPM 耗尽、额度限制）及网络/网关超时问题：
1. **自解释任务指引约束 (`task_instructions.md` 第五节)**：
   - 明确规则：**绝对禁止因限额或超时中断任务**；遇到 429 或超时必须将任务置为**暂停状态 (Pause)**，而非**中断终止 (Abort)**；
   - 规定 Agent 必须每隔 5 分钟或 10 分钟进行一次探测轮询，等到限额解除后自动无缝继续提取后续条目；
   - 规定 Agent 每完成一个条目即持久化追加写入 `result.json`，启动时自动反查已完成 `item_id` 实现断点续传。
2. **CLI 内置韧性执行命令 (`python -m app_core agent execute`)**：
   - 自动遍历 `items.json`，遇到 API 429 限额或 Timeout 异常时，在终端打印友好倒计时提示，进入挂起等待；
   - 默认每隔 300 秒（5 分钟，可通过 `--interval` 自定义为 600 秒即 10 分钟）向模型端发起一次轻量测试探测；
   - 一旦检测到限额恢复，自动打印恢复提示并从中断条目继续处理，零重复、零丢件，全部完成后自动入库。

### 8.4 Agent CLI 交互支持 (`python -m app_core agent` / `AtomicCalculationDB.exe agent`)
在不启动 Qt 界面图形的情况下，命令行直接支持完整的 Agent 交互：
- **任务导出**：
  ```bash
  python -m app_core agent export DOC-PAP-0000-E284F0
  ```
  输出包含任务目录绝对路径与清单统计的 JSON，Agent 可直接基于目录开展分析。
- **状态与进度查询**：
  ```bash
  python -m app_core agent status DOC-PAP-0000-E284F0
  ```
- **韧性自动执行（带限额暂停与5~10分钟轮询恢复）**：
  ```bash
  python -m app_core agent execute DOC-PAP-0000-E284F0 --interval 300 --model GPT-6.2
  ```
- **提炼结果入库（支持全量与增量断点提交）**：
  ```bash
  # 方式 1：直接提交已生成的 result.json（缺省时自动查找任务目录下的 result.json）
  python -m app_core agent submit DOC-PAP-0000-E284F0
  python -m app_core agent submit DOC-PAP-0000-E284F0 path/to/result.json --agent-name Codex --agent-model gpt-5-codex

  # 方式 2：命令行直接传入 JSON 字符串
  python -m app_core agent submit DOC-PAP-0000-E284F0 --json '{"items": [...]}'
  ```

---

## 9. 本地 Agent / CLI 专属图形工作台与数据表格总结入库 (`app_core/ui/agent_task_dialog.py` & `app_core/sync_mcp/agent_task_packager.py`)

### 9.1 执行策略专属选项 (`fast_mode_combo`)
在文献详情工作台顶部的“⚙️ 执行策略: ③ AI 快速提取”下拉列表中新增第三项专属执行策略：
- `🤖 模型调度策略 (API 自动)`（`auto`）：直接调用后台大模型 API 并发提取；
- `📋 网页版 AI 手动 (零 Token)`（`manual`）：打开半自动提问包提炼工作台；
- `💻 本地 Agent / CLI 任务包`（`agent`）：针对本地 autonomous agents（如 Codex、Claude Code、Antigravity）提供完整自解释文件夹、条目审查、表格总结与双按键导入工作台。

当选择 `💻 本地 Agent / CLI 任务包` 并点击“③ 从AI快速提取开始”按键时，无缝呼出 `AgentTaskDialog`。

### 9.2 Agent 图形工作台三区架构 (`AgentTaskDialog`)
1. **顶部控制面板 (Agent 任务包与工作路径)**：
   - 任务包目录展示：显示绝对路径（`workspace/agent_tasks/<DOC-ID>/`），提供 `📋 复制路径`、`📁 打开文件夹`（调用系统 Explorer）与 `🔄 重新生成` 按钮；
   - CLI 命令行：显示执行命令（`python -m app_core agent execute <DOC-ID>`），提供 `📋 复制命令`；
   - 提示词指引：提供 `📋 复制完整 Agent 提示词`，将包含自解释规则、文件用途说明与 429 限额防中断暂停规范的 Prompt 一键复制到剪贴板。
2. **三标签页内容管理与审查**：
   - **一键极简 Agent 交互 (Prompt 绝对路径与规则全内嵌)**：
     - 点击 `📋 复制完整 Agent 提示词` 时，自动在剪贴板提示词头部注入：
       - `【工作目录】`：文献对应的绝对物理文件夹路径（如 `c:\...\workspace\agent_tasks\<DOC-ID>`）；
       - `【目标文献】`：文献 ID 与完整标题；
       - `【执行要求】`：详细告知 Agent 自动访问目录读取 `task_instructions.md`、`items.json`、`tables/` 与 `parsed.md`；
       - `【限额防中断】`：明确要求若遭遇 429 限额或网络超时，每 5~10 分钟轮询等待恢复，切勿直接中断终止任务；
       - `【输出规范】`：任务完成后直接在工作目录下生成 `result.json`；
     - 用户完全无需单独复制文件夹路径或 CLI 命令，只需一键点击复制提示词并粘贴到 Codex 聊天框即可。
   - **Tab 1：📝 物理问题条目 (`items.json`)**：
     - 清晰表格呈现所有条目的 ID、章节路径、对应页码、焦点陈述 (Focal Sentence) 及按需关联的数据表格与配图；
     - 提供 `📋 复制全部条目 JSON` 与 `📋 复制选中条目 JSON`；
     - 底部操作栏提供与网页 AI 一致的双导入按键：
       - `📥 导入回复文件`：选择 Agent 或大模型输出的 JSON/文本文件并自动解析入库；
       - `📝 粘贴回复文本`：弹出专用粘贴回复弹窗（`AgentPasteReplyDialog`），支持选择处理模型并贴入回复内容。
   - **Tab 2：📊 数据表格与总结 (`tables/`)**：
     - 左侧展示文献提取出的所有骨架数据表格列表（`Table_1.md`, `Table_2.md` ...）；
     - 右侧实时预览选中表格的 Markdown 骨架结构；
     - 提供 `📋 复制表格总结提示词`（调用 `build_table_summary_prompt` 自动汇编包含论文元数据、全部表格骨架与输出规范的 Prompt）；
     - 提供 `📋 复制当前表格 Markdown`；
     - 底部操作栏配备专属双导入按键：
       - `📥 导入回复文件`：直接导入 AI 针对数据表格输出的 JSON 总结文件；
       - `📝 粘贴回复文本`：直接粘贴 AI 总结回复文本。
   - **Tab 3：📄 任务包原始文件**：
     - 文件树浏览 `task_instructions.md`、`items.json`、`schema.json`、`parsed.md`、`tables/` 与 `images/`；
     - 支持查看文件内容并一键复制内容或文件路径。
3. **数据表格总结直接生成数据类型知识卡 (DATA Cards)**：
   - AI 输出包含表格表头含义、物理量范围、目标离子/能级、数据规模与数值结论的总结；
   - `AgentTaskPackager.submit_table_summary_result` 自动将回复解析为 `KnowledgeCardDraft`，标记 `category="DATA"`、`data_type="TABLE_SUMMARY"`；
   - 在底座与模型校验中扩展 `_CARD_TYPE_ALIASES` 兼容 `"DATA"`，安全映射到 L2 数据库 `CONC` 数值结论类型并生成唯一 ID，关联文献全景脉络。

### 9.3 文献库顶层任务范围对话框 (`TaskScopeDialog`) 统一增加 Agent / CLI 选项
- **顶栏入口无缝打通**：在文献库顶部的“⚡ ③ AI 快速理解”与“✨ ⑤ AI 深度归纳”按键，以及右键菜单唤起的 `TaskScopeDialog` 中：
  - “选择执行方式”统一扩展为三大并行选项：
    1. `⚡ 依照模型策略自动处理 (按模型调度策略编排器设置自动运行)`
    2. `📋 无API网页版AI半自动模式 (剪切板提问包，零 Token 消耗)`
    3. `💻 本地 Agent / CLI 模式 (Codex/Claude Code/Antigravity 专属任务包与命令行)`
  - 勾选本地 Agent 模式后，状态栏与确认按键动态切换为 `💻 打开 Agent / CLI 工作台`；
  - 确认后直接拉起 `AgentTaskDialog`（第 ③ 步）或 `DeepSynthesisDialog` 的 Codex 沙盒 Agent 标签页（第 ⑤ 步）。

### 9.4 批量文献 Agent 任务包切换与导出支持 (`AgentTaskDialog`)
- 当用户在文献库中勾选/选中多篇文献进入 Agent 任务台时，顶栏自动渲染“切换目标文献 (`QComboBox`)”与“📦 批量导出全部任务包”功能按键；
- 允许用户在同一窗口内平滑切换不同文献，随时随地一键为选中的全部文献生成自解释任务目录，方便统一提供给本地 Agent 批量处理。

### 9.5 结果自动化严格核验与一键打回/入库引擎 (`validate_task_result` & `AgentRejectionDialog`)
- **自动检测与一键核验按键**：在 Agent 工作台顶栏（Row 3）与 Tab 1 底部操作栏均配备高亮按键 **`⚡ 检测并一键验收 result.json`**：
  1. 自动探查任务目录下是否存在 `result.json`，若文件未生成则给出清晰温馨提示与路径指引；
  2. 自动剥离 Markdown 围栏（````json ... ````），校验 JSON 语法，若存在引号或 LaTeX 反斜杠转义问题直接定位；
  3. **条目完整性对比**：依据 `items.json` 比对返回的条目 ID 集合，精准捕获条目遗漏与缺失（例如预期 10 条仅返回 8 条，精准指出缺失条目编号）；
  4. **严格 Schema 校验**：基于 `ExtractionResult` 校验每张知识卡片的标题、核心总结、前提条件与受控类别。
- **自动化打回重做机制 (`AgentRejectionDialog`)**：
  - 若存在任何语法、条目缺失或字段错误，**坚决不写入数据库**，立即唤起专属打回对话框；
  - 红色警示框清晰罗列所有被拒原因与问题清单；
  - **`📋 复制打回重做提示词 (发送给 Agent)`**：一键生成结构化的 Agent 修复 Prompt，直截了当告知 Agent 具体缺失哪些条目、格式错在何处、如何重新覆盖保存，用户直接粘贴发送给 Codex 即可极速修复；
  - 提供 `⚠️ 忽略缺失强制部分入库` 备选容错分支。
- **验收通过一键入库**：
  - 校验通过后，自动调用数据管道完成物理卡片入库、关键词挂载、文献状态跃迁至 `LLM_EXTRACTED`，并无缝刷新所有关联界面。
- **CLI 命令行联动**：支持 `python -m app_core agent verify <DOC-ID> [result_file]`，支持终端与自动化脚本秒级校验。

### 9.6 修复缺陷
- 修复在 `DocumentDetailDialog` 中关闭 Agent 任务工作台后调用不存在的 `_refresh_pipeline_stepper` 导致的 `AttributeError`（统一修正为调用现有的 `self._refresh_stepper()` 与 `self._refresh_related_cards()`）。
- 详情页“⑤ AI 深度归纳”策略下拉框同步补齐 `💻 本地 Agent / CLI 模式` 选项。

### 9.7 任务验收通过后按键状态联动与界面保护机制
- **状态感知与按键联动**：
  - 在 `AgentTaskDialog` 中，当文献处于 `LLM_EXTRACTED` 状态，或用户点击验收按键成功通过严格核验并完成入库后：
    - **`⚡ 检测并一键验收 result.json`**（顶栏 Row 3）与 **`⚡ 自动验收 result.json`**（Tab 1 底部）：按键背景色自动切换为绿色（`#16a34a`），文本更新为 **`✅ 已验收通过`**，并设置为不可再次点击（`setEnabled(False)`），附带明确的通过说明气泡提示；
    - **`📥 导入回复文件`** 与 **`📝 粘贴回复文本`**：自动变为浅灰禁用状态（`setEnabled(False)`），防止在已提取完成的文献上发生误触覆盖写入；
  - 若用户在顶栏点击“🔄 重新生成”，文献重置回初始状态，按键自动恢复为蓝色可用状态。

### 9.8 网页 AI 模式与 CLI 模式全异步防假死数据库写入进度条 (`AsyncProgressDialog`)
- **根除假死冻结体验**：此前在网页版手动提炼 (`SemiAutoBatchDialog`) 与本地 Agent 工作台 (`AgentTaskDialog`) 中，当写入大量知识卡片或同步更新向量数据库索引时，主界面会发生瞬间停顿与假死卡住感。
- **现代异步进度弹窗 (`app_core/ui/progress_dialog.py`)**：
  - 封装基于 `QThread` 的专用后台任务工作器 `TaskWorker` 与模态对话框 `AsyncProgressDialog`；
  - 采用现代柔和圆角卡片、平滑渐变进度条（`#0284c7` 至 `#38bdf8`）与居中图标；
  - 支持双模式动态反馈：默认跑马灯动画防假死，当存在明确条目数或表格数时自动转为步进进度（如 `正在写入数据库... (5/17)` 并显示实时处理的具体条目编号）；
  - 全面包裹：
    1. 网页 AI 提炼批量写入 (`finalize_run`)；
    2. Agent 任务结果一键验收入库与强制部分入库 (`_verify_and_ingest_result_json`)；
    3. Agent 条目文件/文本导入写入 (`_submit_items`)；
    4. 数据表格总结文件/文本导入写入 (`_submit_tables`)；
  - 在保持界面丝滑流畅交互的同时，确保多线程下 SQLite / Qdrant 写入的线程安全与异常安全捕获。

---

## 10. 自动化测试与全量验收

- **测试用例总数**：由原 249 项扩展为 **257 项**；
- **全量回归状态**：`pytest tests/` 257 项测试 100% 全部通过（0 失败，0 错误）；
- **新增 Agent 专项测试 (`tests/test_v310.py`)**：
  1. `test_agent_task_packager_export_and_submit`：端到端验证任务目录导出完整性、状态检查、结果提交（L2 卡片入库、关键词解析、文献状态跃迁）；
  2. `test_agent_workbench_and_cli_dispatch`：验证命令行解析器对 `agent export/submit/status/execute` 子命令的分发、`--base` 与 `--workspace` 参数穿透以及 Headless 独立服务安全退出；
  3. `test_rate_limit_detection_and_execute_resilience`：测试 429 / 限额 / 超时错误精准识别、执行器遇到限额自动挂起暂停、定时轮询探测解除后断点恢复与结果完整入库；
  4. `test_table_summary_prompt_and_submission`：测试数据表格总结 Prompt 生成、表格回复导入解析、生成 `DATA` 知识卡并存入数据库；
  5. `test_agent_task_dialog_ui`：测试 `AgentTaskDialog` 图形工作台三标签页初始化、路径与命令行展示，以及 `DocumentDetailDialog` 中 `fast_mode_combo` 的 3 个执行策略选项；
  6. `test_validate_task_result_pass_and_reject`：测试 `validate_task_result` 针对未检测到文件、空文件、JSON 语法错误、条目缺失、Schema 格式错误以及 100% 通过等各种边界情况的精准拦截与通过机制；
  7. `test_agent_rejection_dialog_and_gui_flow`：测试打回对话框 `AgentRejectionDialog` 渲染、打回提示词复制、自动验收按键触发与文献状态跃迁；
  8. `test_agent_task_dialog_auto_verify` & `test_async_progress_dialog`：测试一键自动验收、验收通过后按钮变绿变为已验收通过、导入回复按键禁用以及 `AsyncProgressDialog` 异步写入进度条驱动逻辑。
- **实例文献验证**：以典型超长数据表文献 `DOC-PAP-0000-E284F0` 实测，成功导出自解释任务包目录、175 个条目、21 个浓缩数据表格，并成功生成 15,045 字符的表格总结专用 Prompt。

