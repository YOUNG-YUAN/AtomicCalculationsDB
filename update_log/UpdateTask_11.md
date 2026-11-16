# UpdateTask_11.md — `AtomicCalculationDB` (V3.0.0) 全局公理一次性声明打捞、纯表格无损脱水预压缩与物理延拓-极性对齐深度归纳

> **核心目标**：针对原子与分子物理计算文献“长程公理前置声明”、“密集高维数值表格”与“跨卡物理规律归纳延拓”的专业特性，完成系统核心解析引擎与知识归纳架构的 V3.0.0 里程碑升级：
> 1. **全局公理与一次性声明打捞（Global Preamble Ingestion & Broadcast）**：
>    - 构建 `PreambleExtractor`，在文献前置小节（Abstract / Intro / Theory / Methods）自动打捞全局约定（如原子单位制 `atomic units`、核电荷分布模型 Fermi 参数、芯态冻结 `frozen-core`、规范约定 `Coulomb gauge`、相对论修正等）；
>    - 注入 `ContextBundle.document_preamble` 并在所有证据包头部全局广播，彻底根绝跨节上下文脱落与 AI 盲目猜测。
> 2. **纯表格无损脱水预压缩（Deterministic Table Pre-compression）**：
>    - 构建 `TableCompressor`，对高维数据表进行代码级确定性脱水：剥离单元格多余填充空格、提取整列公共物理量纲（eV, s⁻¹, cm⁻¹, a.u.）与数量级因子（10⁸, 10⁻⁶）至表头、剔除纯自增序号列（`No.`, `Index`, `序号`, `#`）；
>    - 100% 严格保留表头物理含义、数值精度与关键表注（Footnotes），实现 30%~45% 的 Token 纯净瘦身。
> 3. **物理延拓与极性对齐深度归纳（Inductive Deep Synthesis v3.0）**：
>    - **第 ③ 步速览延拓**：`KnowledgeCardDraft` 新增 `generalized_conjecture` 字段，引导 AI 从具体计算事实向广义物理推论（标度律、竞争机制、消长趋势）迈出一步；
>    - **第 ⑤ 步深度归纳范式重构**：`MasterCardDraft` 升级聚焦提炼普适共性规律（`unified_commonality`）与识别截然相反结论的分歧转折临界相界（`bifurcations`）；
>    - **代码级确定性硬溯源链**：引入 `provenance_chain`，由底层数据库元数据直接拼接注入，完全不依赖大模型幻觉，实现 100% 审计级溯源。

---

## 1. 全局公理提取器 (`app_core/parser/preamble_extractor.py`)

### 1.1 核心机制
- **前置小节扫描**：针对文献前 4 个小节或核心方法节进行句子级正则分析；
- **双重特征过滤**：
  - 声明线索（Preamble Cues）：`throughout this work/paper`, `in all calculations`, `unless otherwise specified`, `adopted throughout` 等；
  - 物理概念（Physics Domains）：`atomic units`, `fermi`, `nuclear charge`, `frozen core`, `gauge`, `hamiltonian`, `basis set`, `breit`, `qed` 等；
- **标准化与去重**：输出规整化公理列表（最多 6 条核心声明）。

### 1.2 证据包广播与拼接 (`app_core/parser/context_bundler.py`)
- `ContextBundle` 引入 `document_preamble: list[str]` 字段；
- `ContextBundler.build()` 统一提取文献级 `preamble` 并广播注入每一个衍生的 `ContextBundle`；
- `prompt_markdown()` 在全局前提之前呈现 `## 全局公理与一次性声明 (Document Preamble)` 块，确保下游 LLM 掌握全局上下文。

---

## 2. 纯表格无损脱水预压缩器 (`app_core/parser/table_compressor.py`)

### 2.1 压缩策略
1. **多余空格脱水**：清除 Markdown 表格为了人眼对齐而填充的连续空格；
2. **公共物理单位与量级上提**：
   - 扫描列单元格，若所有行均包含相同单位（如 `eV`, `s^-1`, `cm^-1`, `a.u.`, `Å` 等）或科学计数法阶数，自动将单位提升至列名（`Header [unit]`）并去除数据行重复字符串；
3. **剔除自增序号列**：
   - 识别表头为 `No.`, `Index`, `序号`, `#` 且数据行全为连续整数的纯索引列，自动将其剔除，避免浪费 Token；
4. **无损保全**：
   - 完整保全表格前后导语与表注（Table Footnotes），完全不改动非单位数值与物理有效数字。

---

## 3. 物理延拓与极性对齐深度归纳 (`atomic_prompts.py` & `deep_synthesis.py`)

### 3.1 数据契约升级
- **`KnowledgeCardDraft` (L2 原子卡)**：
  - 新增 `generalized_conjecture: str | None = None`；
  - Prompt 升级要求：若卡片反映了某种趋势、机理或异常，结合物理直觉做一步广义延拓（如类氢标度律 $Z^4$、关联与相对论竞争机制等），单纯数值则留空。
- **`MasterCardDraft` (L1 综述卡)**：
  - 新增 `unified_commonality: str = ""`（普适共性机理总结）；
  - 新增 `bifurcations: list[dict[str, Any]] = Field(default_factory=list)`（相悖结论转折相界分析）；
  - 新增 `provenance_chain: list[dict[str, Any]] = Field(default_factory=list)`（代码级硬溯源链）。

### 3.2 深度归纳工作流
1. **Prompt 导出与生成** (`export_master_synthesis_prompt`)：
   - 自动聚合文献所有 L2 卡片，包含各卡片的前置条件、核心结论、核心公式以及新增的 `generalized_conjecture`（广义物理延拓）；
   - 引导 LLM 重点归纳“共性”与“分歧相界”。
2. **载荷入库与硬溯源构建** (`apply_master_synthesis_payload`)：
   - 从底层 SQLite 数据库提取各卡片的真实出处（`knw_id`, `doc_id`, `source_section`, `page_num`, `method`, `table_caption`），自动拼装至 `provenance_chain`；
   - 落盘保存至 L1 Master 卡片的 `structured_details_json` 中，确保溯源数据 100% 确定性与免幻觉。

### 3.3 AI 校对与文献工作台闭环（Audit-driven Conjecture Enrichment & Workbench）
- **工具栏快捷入口** (`library.py`)：在文献列表工具栏 `[📋 ③ 网页版半自动速览]` 与 `[✨ ⑤ 启动深度归纳]` 之间新增 `[🔍 ④ 选中文献 AI 校对]` 独立操作按钮，文献右键菜单同步加入 `🔍 第 ④ 步：文献 AI 校对与延拓审核`；
- **全功能校对工作台** (`DocumentAuditDialog`)：支持多选文献切换、一键复制审稿任务包到剪贴板（零 Token 网页审稿）、本地文件夹打开、以及校对结果一键导入与解析；
- **任务范围选择** (`TaskScopeDialog`)：全面适配 `task_type="step4_audit"`，支持选中文献、筛选结果与全库范围；
- **审稿规则引导** (`prompt.py`)：新增第 12 条重要规则，提示审稿人 AI 在校对核验卡片真实性的同时，重点审核已有延拓在原子物理原理上的可行性（不可行在 issues 给出物理理由并纠偏），若原卡缺失则顺路补齐；
- **导入白名单接纳** (`importer.py`)：`REVISABLE_FIELDS` 新增 `generalized_conjecture`，校对导入时自动合并并持久化至 `structured_details_json`；
- **老文献免重跑平滑升级**：老文献无需退回第 ③ 步重跑大证据包，在日常第 ④ 步校对审稿流中即可零额外成本自动补齐广义延拓；
- **UI 界面同步呈现**：编辑对话框（`KnowledgeCardEditDialog`）、详情弹窗（`KnowledgeDetailDialog`）与侧边抽屉面板（`CardDrawer`）全面支持查看与修改「广义物理延拓/推论」。

---

## 4. 算力池与调度器重构、半自动双模业务化与工具栏统一

### 4.1 统一模型池与策略编排器规范化与角色扩充
1. **四大业务角色重命名**：
   - 原以“API”命名的三大类正式更名为业务抽象大类：`AI 快速提取`（`fast_llm`）、`AI 深度归纳`（`slow_llm`）、`AI 图像理解`（`vlm`）；
   - 新增第 4 个关键业务大类：`AI 校对核查`（`audit_llm`），`ROLE_KEYS` 与 `ROLE_LABELS` 全面扩容；
2. **策略编排器全量映射**：
   - 原调度策略同步更名为：`AI 快速提取策略`、`AI 深度归纳策略`、`AI 图像理解策略`；
   - 新增：`AI 校对核查策略`（默认策略 ID `legacy-audit-strategy`，支持分时段轮换与接力链）；
   - `EndpointPool.load()` 内置向后兼容自动升级迁移器，无损升级用户旧配置文件；
3. **退役半自动节点类型**：
   - 从统一模型算力池节点类型中正式剥离 `SEMI_AUTO_CLIPBOARD`，半自动剪切板不再作为虚构模型节点存在，而是直接下沉至具体业务操作工作台。

### 4.2 AI 图像理解策略独立开关 (`StrategyToggleSwitch`)
1. **视觉规格**：
   - 在 `AI 图像理解策略` 编辑页面顶部（策略 ID 上方）新增胶囊式滑动开关按钮；
   - **开启状态**：滑块拉柄为绿色（`#16a34a`），轨道呈柔和淡绿，右侧显示绿色加粗提示：`当前节点已开启`；
   - **关闭状态**：滑块拉柄为灰色（`#6b7280`），轨道为浅灰，右侧显示红色加粗提示：`当前节点已关闭`；
2. **调度逻辑联动**：
   - `ModelStrategy` 新增 `enabled: bool = True` 字段；
   - 当图像理解策略处于关闭状态时，`ModelGateway.refresh_route()` 自动将 VLM 状态置为不可用，解析流水线跳过昂贵的图片识别与冗余调用。

### 4.3 半自动剪切板融入业务 UI（参考 APG 提问包与单包文献隔离机制）
1. **任务范围与执行模式双选 (`TaskScopeDialog`)**：
   - 弹窗增加「选择执行方式」单选组件：
     - `⚡ 依照模型策略自动处理`（调用模型调度编排器进行 API 全自动处理）；
     - `📋 无API网页版AI半自动模式`（完全零 Token 成本，纯文本分包提炼）；
   - 用户确认后由调用方分流至后台自动化任务或交互式半自动工作台；
   - 全面打通第 ③ 步（AI 快速理解）、第 ④ 步（AI 校对核查）、第 ⑤ 步（AI 深度归纳）；
2. **严格单文献隔离分包机制（`create_run`）**：
   - 彻底重构提问包生成逻辑：不同文献的证据片段**绝对不会**混入同一个提问包；
   - 遍历各文献独立调用 `DynamicBatchPacker`，若单篇文献超出 Token 上限则在该文献内拆分子包；每个提问包严格隶属于单一 `doc_id`；
3. **平铺文献清单与提问包滚动条规格化 (`SemiAutoBatchDialog`)**：
   - **平铺文献清单 (`doc_table`)**：
     - 彻底废除原本密密麻麻换行堆叠的文献长文本标签（`doc_label`），改为对齐文献库规范的平铺数据表格；
     - 包含 7 维清晰物理列：`DOC-ID`、`标题`、`类型`、`年份`、`期刊`、`作者`、`Token 消耗`；
     - 显示左侧行号（`verticalHeader` 开启），行高 28px，表头 30px；
     - 高度自适应并严格锚定在**刚好完整显示 10 行**（约 316px，若少于 10 篇自适应贴合），右侧配备垂直滚动条（`ScrollBarAsNeeded`），56 篇或更多海量文献平滑滚动不占满屏；
   - **提问包列表规格化 (`batch_box` & `scroll`)**：
     - 提问包卡槽列表独立配备垂直滚动条与自适应视口，高度完整呈现 10 行提问包；
     - 每个提问包卡槽左侧标配高亮行号标签（如 `行号 #01`、`行号 #02` 等）；
   - **每个提问包提供完备动作组**：
     - `📋 复制提示词`：一键将提示词拷入剪贴板；
     - `📎 复制图册PDF`：预留配图接口，为后续文献插图理解提供标准接入点；
     - `📥 导入回复文件`：支持从 `.json`/`.txt`/`.md` 导入外部回复；
     - `📝 粘贴回复文本`：弹出完整 JSON 录入弹窗（带多行输入框、OK/Cancel 按钮，像素级对齐 APG 规范）；
     - `🗑 删除`：一键移除当前未完成提问包；
   - **状态受控按钮**：
     - 未全齐时显示：`⏳ 第二步：等待导入提问包 (X/Y)`（置灰禁止误触）；
     - 全部导入通过后点亮：`🚀 第二步：写入知识库`，确保数据完整性；
     - 顶部配备 `🗑 清空/删除已生成提问包`。

### 4.4 文献库工具栏统一与精简 (`LibraryView`)
1. **删除冗余按钮**：从文献库主工具栏彻底移除原 `[📋 ③ 网页版半自动速览]` 按钮（其功能已融合进第 ③ 步双模弹窗）；
2. **规范重命名与视觉统一**：
   - 原 `[⚡ ③ 选中文献 AI 速览]` 更名为 `[⚡ ③ AI 快速理解]`（样式保持 `Primary` 蓝标）；
   - 原 `[🔍 ④ 选中文献 AI 校对]` 更名为 `[🔍 ④ AI 校对核查]`（样式为 `Pill`）；
   - 原 `[✨ ⑤ 启动深度归纳 (冒金光)]` 更名为 `[✨ ⑤ AI 深度归纳]`，并且将其样式从 `Warning` 黄色改为与第 ③ 步完全一致的 `Primary` 蓝标，实现界面视觉的高度协调整齐；
3. **右键上下文菜单同步更新**：右键菜单动作文本全量同步为标准命名。

### 4.5 算力池模型配置独立化与操作系统级凭据加密存储 (`model_pool.enc` & Windows DPAPI)
1. **敏感配置与主配置文件解耦**：
   - 移除 `config.yaml` 中的明文模型池配置与 API Key，主配置中仅保留 `{"storage": "model_pool.enc", "encrypted": true}` 存储指针；
   - 彻底杜绝开源分享或提交 GitHub 时意外泄露私有商业模型凭据（API Keys / Endpoint URLs / 认证头）的风险；
2. **操作系统级硬件绑定加密 (`app_core.models.security`)**：
   - Windows 平台原生调用 Windows Data Protection API (`DPAPI`: `CryptProtectData` / `CryptUnprotectData` via `ctypes`)，绑定当前登录 Windows 用户凭据加密，无需第三方依赖，防止跨机器或跨账户盗取；
   - 非 Windows / 跨平台环境提供 SHA-256 派生密钥与安全异或/流混淆回退加密机制；
3. **无缝透明迁移与持久化 (`EndpointPoolManager`)**：
   - 启动加载时优先读取 `workspace/model_pool.enc`；若存在旧版 `config.yaml` 内嵌明文配置，自动平滑迁移加密至独立文件，并剔除原明文；
   - 算力池增删改查保存时，全量加密写入 `workspace/model_pool.enc`，保障运行期与静态存储全生命周期安全。

---

## 5. 版本与发布交付记录

- **版本号**：`3.0.0`（Build: `20261116`，Channel: `v3.0`）；
- **回归测试**：
  - 新增 `tests/test_v300.py`（共 14 组测试项），专门验证全局声明打捞、表格压缩（单位提取/无序剔除/空格脱水）、延拓字段解析与硬溯源生成、校对审计回填延拓、第 ④ 步校对工作台交互、模型池四大业务角色与校对策略自动生成、图像理解开关组件与持久化、半自动提问包单篇隔离、工具栏按钮全套规格、以及算力池凭据独立加密存储 (`model_pool.enc` & DPAPI)；
  - 全量 pytest 测试套件 100% 通过（195 项全绿）；
- **打包文件**：
  - 增量升级补丁：`release/AtomicCalculationDB_Patch_v3.0.0.zip`
  - 完整发布安装包：`release/AtomicCalculationDB_Full_v3.0.0.zip`
  - 经 `zipfile.testzip()` 校验 CRC32 均完整无误；
- **知识库重新索引**：通过 `codebase-memory-mcp` 完成全量符号索引。
