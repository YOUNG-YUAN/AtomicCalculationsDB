# FixTask_03_02.md — `AtomicCalculationDB` (V2.2.2) 程序手册与计算实例深度提取、省 Token 三档分级重处理机制、ONNX 全链路赋能与 Zotero 文件名解析任务书

> **致 AI Coding Agent 的全局执行说明**：
> 1. 在测试导入《GRASP2018 官方指南/CPC 论文》（`Froese Fischer 等 - 2019 - GRASP2018—A Fortran 95 version of the General Relativistic Atomic Structure Package.pdf`）时，发现虽然生成了 `parsed.md`，但提取出的关联知识卡为 `0`（因普通论文结论探针与 `skip` 规则误杀了“程序手册/实操指南”体裁中的计算实例、工作流与输入脚本，且长文档前面的安装/目录章节消耗了提取配额）。
> 2. 用户需要对库内已处理的存量文献进行重新修复与提炼，但**必须避免无脑全删重跑导致云端大模型 Token 被重复消耗一遍**。
> 请严格按以下 5 项任务完成升级，跑通全部验证测试并重新构建 `AtomicCalculationDB.exe`。

---

## Task 1：实现省 Token 的“三档分级存量文献重新处理”机制与单篇控制弹窗

为了在修复存量文献、补提漏网卡片的同时，最大限度为用户节省云端大模型 Token，必须实现**“人工修改保护锁 + 切片指纹缓存（Chunk Hash Cache）+ 三档分级重处理”**：

### 1.1 人工校准保护锁与切片指纹缓存 (`sqlite_manager.py` & `context_bundler.py`)
1. **人工校准卡片保护 (`is_user_verified`)**：
   * 在 `knowledge_cards` 表中安全新增字段：`is_user_verified INTEGER DEFAULT 0`。
   * 凡是用户通过【✏️ 手动编辑】或【✨ AI 修改】保存过的知识卡片，自动将 `is_user_verified` 设为 `1`。
   * **重跑保护铁律**：在执行任何级别的文献重新处理时，清理旧自动生成卡片（`is_user_verified = 0`）的同时，**必须 100% 保留 `is_user_verified = 1` 的人工校准卡片，严禁覆盖或删除用户手动修改过的成果**！
2. **证据包切片指纹缓存 (`bundle_hash`)**：
   * 在 `raw_evidence_chunks` 表中安全新增字段：`bundle_hash TEXT`（基于 `ContextBundle` 核心文本规范化后计算 SHA256 哈希，并建立索引）。
   * 凡是已成功提炼出有效知识卡的证据包，记录其 `bundle_hash`，供增量补提时直接比对跳过。

### 1.2 在【文献库】顶部工具栏提供明码标示 Token 消耗的 3 档重处理按钮 (`views/library_view.py`)
1. **🟢 第一档：`[🧹 全库本地修复与清洗 (0 Token)]`（纯本地 CPU 执行，零 API 消耗）**
   * 点击后**不调用任何云端大模型**，纯本地秒级完成以下 4 件事：
     1. **修复全库文献元数据**：使用升级后的 Zotero 文件名解析器、首页 DOI/PII 提取器与本地 ONNX OCR，自动修正全库所有标题为 `untitled`、`PII:...`、含乱码符号（如 `C ~ g J ! 5 (`、`H DC = □`）或年份为 `0000` 的文献标题、作者、年份与期刊。
     2. **清理无效废卡**：自动清除库内标题含有“证据截断”或正文声明“无法可靠重建具体计算策略”的残缺废卡。
     3. **本地净化公式与伪影**：自动扫描已有卡片的 `formulas_latex` 字段，将混入其中的英文解释长句（如 `"The focus fragment contains..."`）剥离回正文，并清除 `vuuut1` 等老 PDF 字体映射伪影。
     4. **本地 ONNX 重新聚类与争议打标**：读取 SQLite 现有卡片，调用本地已激活的 `all-MiniLM-L6-v2 (ONNX)` 与 `nli-deberta-v3-xsmall (ONNX)` 重新执行跨文献同义合并（更新关联文献数）与 `⚡ 争议` 校验。
2. **🟡 第二档：`[⚡ 智能增量补提 (省 Token)]`（推荐：仅对 0 卡片文献与新发现片段耗 Token）**
   * 遍历库内文献生成候选 `ContextBundle` 时，先检查 `bundle_hash` 是否已存在于 `raw_evidence_chunks` 且关联了有效卡片：
     * **若已存在**：**直接跳过，严禁重复发送给大模型消耗 Token**！
     * **仅对以下两类内容调用大模型提炼**：
       1. 当前关联知识卡数量为 `0` 的文献（如刚导入的《GRASP2018 指南》）；
       2. 因本次升级“程序手册与计算实例探针”而在老文献中新捕获到的、此前从未提炼过的增量实例/脚本片段。
3. **🔴 第三档：`[🔥 强制重提选中文献 (耗 Token)]`**
   * 仅针对用户在文献表格中勾选的特定文献，清除其未人工校准的旧卡片（保留 `is_user_verified = 1` 的卡片），重新调用大模型从头提炼。
   * **Token 预估防呆确认框**：点击后先根据目标文献的候选证据包总字符数估算输入 Token 量，弹出圆润确认框：*“强制重新提炼选中的 N 篇文献预计将消耗约 X,XXX 输入 Token（您手动编辑过的卡片将被安全保留），是否继续？”*，用户确认后方可执行。

### 1.3 在双击打开的【文献详情】弹窗中新增单篇控制栏
* 在弹窗内“打开 PDF”、“打开存储文件夹”旁新增：
  * **文献类型切换下拉框**（`论文` / `程序手册` / `专著` / `综述`，切换后实时更新数据库）；
  * **`[⚡ 智能补提知识卡]`** 与 **`[🔥 强制重新提炼]`** 按钮；
  * 实时显示提取进度条与动态日志，完成后立即刷新弹窗底部的 **【关联知识卡 · N】** 列表（支持在弹窗内点击卡片标题直接打开卡片详情）；
  * 若某篇文献关联知识卡为 `0`，必须在弹窗内显示明确的诊断原因（如：`仅完成了文本转换，尚未执行知识卡提炼` 或显示具体过滤/接口异常信息）。

---

## Task 2：新增“程序手册/实操指南”专用探针与提取模式 (`four_channel_radar.py`, `context_bundler.py`, `atomic_prompts.py`)

1. **自动识别程序手册与方法指南 (`doc_type = 'MAN'` 或含指南特征的文献)**：
   * 当文献标题、文件名或章节标题包含 `GRASP`, `FAC`, `AMBiT`, `DIRAC`, `BSR`, `guide`, `manual`, `package`, `program`, `Computer Physics Communications`, `version of` 等特征时，自动启用 **【手册与实操实例增强模式】**。
2. **扩充四通道探针——精准捕获手册中的“计算经验”与“计算实例”**：
   * **严禁以“必须包含实验误差百分比对比”为由拦截手册/指南内容！** 在手册与指南中，凡命中以下任一特征的段落或小节，必须 100% 打包为 `ContextBundle`：
     1. **计算实例与案例教学（Worked Examples / Case Studies）**：小节标题或正文含 `Example`, `Case study`, `Sample run`, `Practical guide`, `Test case`, `Illustrative calculation`。
     2. **实操经验与收敛法则（Practical Rules of Thumb & Convergence Tips）**：包含指导性句式，如 `In practice`, `It is advisable / important / recommended to`, `layer by layer`, `hold ... fixed`, `spectroscopic orbitals`, `correlation orbitals`, `to avoid convergence`, `weighting`, `EOL / EAL`, `zero-order space`。
     3. **程序工作流与子程序职责（Program Workflow & Modules）**：提及具体模块调用顺序与职责（如 `rnucleus`, `rcsfgenerate`, `rcsfinteract`, `rwfnestimate`, `rmcdhf`, `rci`, `rbiotransform`, `rtransition`, `rhfs`, `ris4`, `jj2lsj`）。
     4. **交互式输入流与批处理脚本（Input Streams & Scripts）**：包含终端交互问答示例、代码块、`<< EOF` 脚本或组态生成规则语法（如 `{2s, 2p}`, `SD excitations`）。
3. **自动跳过无物理价值的“编译安装与版权声明”章节，放开全篇章节遍历**：
   * 在打包 `ContextBundle` 时，自动跳过标题为 `Installation`, `Compilation`, `Directory structure`, `License`, `Acknowledgments`, `References`, `Table of Contents` 的纯软件工程章节；
   * **严禁只取前几个切片就提前终止循环！** 对手册/长文中的所有核心物理与实例章节进行完整遍历，确保生成完整的【计算策略卡 (`STRAT`)】、【程序与代码卡 (`CODE`)】、【物理模型卡 (`MODEL`)】和【排错踩坑卡 (`PITFALL`)】。

---

## Task 3：确保本地 ONNX 模型在重处理全流程中 100% 深度参与 (`model_registry.py` & `parser/`)

在执行文献（重新）处理管线时，确保已激活的本地 ONNX 模型在以下 4 个关键节点真正发挥作用（并在工作台日志中记录 ONNX 参与状态）：
1. **节点 1（乱码页/纯图页本地 OCR 还原）**：当首页标题提取遇到字体编码损坏（如 `C ~ g J ! 5 (`、`H DC = □`）或遇到无文本层的纯图页面时，自动调用本地 OCR / VLM 还原真实标题与正文公式。
2. **节点 2（候选证据包 ONNX 语义相关性初筛与排序）**：当长文献/手册切分出大量候选片段时，调用本地 `all-MiniLM-L6-v2 (ONNX)` 计算各片段与“原子物理计算策略/物理模型/程序工作流”锚点向量的余弦相似度，优先提炼高价值密度片段，节省云端 LLM Token。
3. **节点 3（跨文献同义卡片 ONNX 快速聚类合并）**：新卡片入库前，调用本地 `all-MiniLM-L6-v2 (ONNX)` 与库内已有同体系/同方法卡片计算语义相似度；若相似度达到同义阈值且无极性冲突，自动合并并累加 `consensus_count`（关联文献数 `+1`），避免生成重复卡片。
4. **节点 4（跨文献极性冲突与争议 NLI 探测）**：对同主题卡片对调用本地 `nli-deberta-v3-xsmall (ONNX)` 校验蕴含与矛盾关系；一旦判定为 `Contradiction`（或极性相反），自动标记 `has_conflict = 1`（点亮卡片上的 `⚡ 争议` 徽章）。

---

## Task 4：支持 Zotero / Mendeley 标准文件名元数据解析 (`metadata_extractor.py`)

* **修复要求**：
  * 当本地导入的 PDF 文件名匹配文献管理软件导出格式：`^(?P<authors>.+?)\s*[-_]\s*(?P<year>19\d\d|20\d\d)\s*[-_]\s*(?P<title>.{12,})\.pdf$`（例如 `Froese Fischer 等 - 2019 - GRASP2018—A Fortran 95 version of the General Relativistic Atomic Structure Package.pdf`）时，优先提取文件名中的 `authors`、`year` 和 `title` 作为基准元数据（同时结合正文 DOI 补全期刊名称如 `Computer Physics Communications`）。
  * 在启动迁移或点击 `[🧹 全库本地修复与清洗 (0 Token)]` 时，自动修正库内所有符合该文件名格式或存在乱码标题的文献元数据。

---

## Task 5：验证与重新打包 `AtomicCalculationDB.exe`

1. 运行 `pytest` 验证：
   * `is_user_verified = 1` 的手动修改卡片在重处理时 100% 受到保护不被误删；
   * `bundle_hash` 切片指纹缓存在 `[⚡ 智能增量补提]` 模式下能够准确跳过已提炼片段，实现零重复 Token 浪费；
   * `[🧹 全库本地修复与清洗 (0 Token)]` 在零大模型调用下成功修复 Zotero 文件名标题（如 GRASP2018 2019 论文）、清理废卡与公式字段英文残留；
   * 针对 `DOC-PAP-2018-1B71B2`（GRASP2018 指南）执行补提后，成功生成多张涵盖工作流、逐层优化策略与输入脚本的知识卡（关联知识卡数量 `> 5`）。
2. 重新编译生成 **`AtomicCalculationDB.exe`** 并更新验证截图。