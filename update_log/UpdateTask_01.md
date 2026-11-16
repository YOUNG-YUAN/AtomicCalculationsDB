# UpdateTask_01.md — `AtomicCalculationDB` (V2.0) 核心架构与全栈实现任务书

> **致 AI Coding Agent 的全局执行守则（必读）**：
> 1. **项目定位**：`AtomicCalculationDB` 是一个面向计算原子物理（MCDHF, RCI, RMBPT, CC, CI+MBPT 等）的全自动文献挖掘、物理模型剖析、计算策略提炼与代码知识库桌面系统。
> 2. **轻量化打包铁律**：严禁在基础运行环境中引入庞大的 `torch`（PyTorch）依赖！所有本地内置轻量化模型（分句、句法树、同义聚类、NLI 冲突检测、Reranker、本地兜底 Embedding）必须全部基于 **`onnxruntime` + `tokenizers` + `spacy` + `PyMuPDF (fitz)`** 在纯 CPU 上运行。
> 3. **壳核分离架构**：所有第三方库由顶层 `launcher.py` 承载并用于编译 `.exe`；所有业务逻辑代码必须置于 `app_core/` 目录下，支持在 GUI 内通过导入 `.zip` 更新包实现免重编译增量热更新。
> 4. **全本地文件化存储**：所有数据库（`SQLite3 (含 FTS5)` + `Qdrant Local Mode`）与文献附件均保存在 `workspace/` 目录下，无需安装任何外部数据库服务端（免 Docker）。

---

## 一、 项目标准目录树 (Directory Structure)

请首先在工作区创建以下完整的目录与文件骨架：

```text
AtomicCalculationDB/
├── launcher.py                          # [编译入口] 壳程序：负责增量更新包解压覆盖、异常回滚并动态加载 app_core
├── requirements.txt                     # 依赖清单（严禁包含 torch，统一使用 onnxruntime）
├── UpdateTask_01.md                     # 本任务指令书
│
├── app_core/                            # [热更新核心区] 所有业务逻辑代码
│   ├── __init__.py
│   ├── version.json                     # {"version": "2.0.0", "build": "20261031", "schema_ver": 2}
│   ├── main_entry.py                    # 应用程序主入口（初始化 Workspace、启动后台调度与 PySide6 GUI）
│   │
│   ├── builtin_models/                  # [内置轻量模型存放区]
│   │   ├── model_registry.py            # 管理内置 ONNX 模型与 spaCy 模型的加载、自动下载校验与单例缓存
│   │   └── README_MODELS.md             # 记录内置模型清单与本地 ONNX 路径映射
│   │
│   ├── db/                              # [存储与三步检索引擎]
│   │   ├── id_generator.py              # 双层全局唯一 ID 生成器 (DOC-xxx, KNW-xxx, KW-xxx, MASTER-xxx)
│   │   ├── sqlite_manager.py            # SQLite3 + FTS5 全文倒排索引 + 任务状态账本 + 符号标准化表
│   │   ├── qdrant_manager.py            # Qdrant 本地文件夹模式封装 (1024维 BGE-M3 HNSW 图索引 + Payload 索引)
│   │   └── hybrid_retriever.py          # 三步检索引擎：1.元数据硬过滤 -> 2.FTS5+Qdrant混合召回Top50 -> 3.Rerank+权威加权Top10
│   │
│   ├── crawler/                         # [文献寻源与多级瀑布流下载器]
│   │   ├── discovery_api.py             # OpenAlex / Semantic Scholar / arXiv / Crossref 引文滚雪球寻源
│   │   ├── title_gatekeeper.py          # 下载前 3 秒守门员：标题 4 槽位拆解与一票否决正则评分器
│   │   ├── waterfall_fetcher.py         # 4级下载：1.arXiv源码/PDF -> 2.校园网IP(Playwright直连) -> 3.Sci-Hub -> 4.待手动下载清单
│   │   └── manual_inbox_watcher.py      # 监控拖拽与 workspace/inbox_manual/ 文件夹，自动识别 PDF 匹配销账
│   │
│   ├── parser/                          # [零成本 CPU 预处理与三点式上下文打包引擎]
│   │   ├── pdf_to_markdown.py           # PyMuPDF(fitz) 极速块解析 + RapidLatexOCR(ONNX) + 外部 MinerU/VLM 路由
│   │   ├── book_locator.py              # 长专著定位：目录/书末索引(ToC/Index)路由 + 动态关键词 FTS5 倒排狙击
│   │   ├── four_channel_radar.py        # 四通道探针：A.结论与转折句(spaCy) B.策略收敛小表vs数据大表 C.公式算符块 D.代码参数块
│   │   ├── context_bundler.py           # 核心！三点式上下文溯源缝合：[小节首段大前提] + [指代/表格回溯] + [结论局部窗口]
│   │   ├── conclusion_tagger.py         # 归属过滤(剔除引文[12]/et al.) + 原子物理六维结论类型多标签打标(Bitmask)
│   │   └── consensus_conflict_engine.py # 内置 MiniLM 快速图聚类(同义合并+共识度累加) + 内置 NLI 极性冲突雷达(⚡争议预警)
│   │
│   ├── models/                          # [统一 AI 模型网关与物理 Prompt 库]
│   │   ├── gateway.py                   # 统一封装 FastLLM, SlowLLM, VLM, BGE-M3 Embedding(本地/云端互通), Reranker
│   │   └── atomic_prompts.py            # 针对策略卡、模型卡、代码卡、冲突会诊与 L1 总纲生成的专用 Prompt 模板与 Pydantic Schema
│   │
│   ├── scheduler/                       # [夜间无人值守自动驾驶工作台]
│   │   ├── night_worker.py              # 定时启停控制器(如 18:00-08:00)、断点续传状态机、优雅停机(Graceful Shutdown)
│   │   └── morning_reporter.py          # 凌晨慢脑跨文献归纳(生成 L1 总纲)与《早安科研简报》Markdown 生成器
│   │
│   ├── sync_mcp/                        # [数据迁移、云同步与 MCP 服务端]
│   │   ├── acdb_packager.py             # .acdb 知识包导出与基于唯一 ID 的增量合并导入
│   │   ├── cloud_sync.py                # WebDAV (webdav4) 与 S3 (boto3) 增量快照同步引擎
│   │   └── mcp_server.py                # 基于 FastMCP 的本地 MCP Server (供 Claude/Cursor 直接检索调用)
│   │
│   ├── updater/                         # [应用热更新模块]
│   │   └── patch_manager.py             # 校验用户导入的更新包 (.zip) 并触发 launcher.py 重启替换
│   │
│   └── ui/                              # [Google Material Design 3 简约圆润 GUI]
│       ├── m3_theme.py                  # Google Material You 色板、大圆角 QSS 样式表与动画组件库
│       ├── main_window.py               # 无边框圆角主窗口 + 左侧胶囊导航栏 (Navigation Rail)
│       └── views/                       # 五大核心视图：1.探索中心 2.夜间工作台 3.文献与待下清单 4.同步与MCP 5.设置与更新
│
└── workspace/                           # [全量用户数据区] (运行时自动初始化)
    ├── config.yaml                      # 全局配置文件
    ├── atomic_meta.sqlite               # SQLite 主数据库 (含 FTS5 全文索引)
    ├── vector_db/                       # Qdrant 本地向量库目录
    ├── inbox_manual/                    # 手动下载 PDF 监控目录
    ├── morning_reports/                 # 每日早安简报存档
    └── storage/                         # 文献源文件存档 (按 DOC-ID 分目录存放 PDF/LaTeX/MD)
```

---

## 二、 依赖环境清单 (`requirements.txt`)

```txt
# GUI & 视觉渲染
PySide6>=6.6.0
pygments>=2.17.0
markdown>=3.5.0

# 轻量 NLP 与本地 CPU 推理底座 (严禁引入 torch)
spacy>=3.7.2
onnxruntime>=1.17.0
tokenizers>=0.15.0
numpy>=1.26.0
PyMuPDF>=1.23.26

# 数据库与混合检索
qdrant-client>=1.8.0
pydantic>=2.6.0
pyyaml>=6.0.1

# 爬虫与自动化下载
httpx[http2]>=0.27.0
playwright>=1.42.0
beautifulsoup4>=4.12.3
lxml>=5.1.0

# 定时调度、云同步与 MCP 协议
APScheduler>=3.10.4
webdav4>=0.9.8
boto3>=1.34.0
mcp>=1.2.0
openai>=1.14.0
```

---

## 三、 核心模块实现规范（Agent 编码细则）

### 模块 1：数据库 Schema 与双层唯一 ID 体系 (`app_core/db/`)

#### 1.1 双层唯一 ID 规范 (`id_generator.py`)
所有入库实体必须生成全局唯一、带语义前缀的定长字符串 ID（用于精确检索、引用跳转及 `.acdb` 跨库增量合并）：
* **元文件 ID (`doc_id`)**：`DOC-{TYPE}-{YEAR}-{HASH6}`
  * `TYPE` 取值：`PAP` (论文), `BOK` (专著), `REV` (综述), `MAN` (程序手册)。
  * `HASH6`：基于标准化的 `DOI`（若无 DOI 则用小写去空格标题）计算 SHA256 取前 6 位大写十六进制（确保同一篇文献在任何机器上生成的 `doc_id` 恒定一致！）。
* **元知识 ID (`knw_id`)**：
  * **L1 顶层总纲卡**：`MASTER-{ISOELECTRONIC_OR_TOPIC}-{METHOD}`（如 `MASTER-NELIKE-MCDHF`）
  * **L2 元知识卡片**：`KNW-{CARD_TYPE}-{YEAR}-{HASH6}`
    * `CARD_TYPE` 取值：`STRAT` (计算策略卡), `MODEL` (物理模型卡), `CODE` (程序与脚本卡), `PITFALL` (排错与踩坑卡), `CONC` (核心结论卡)。
* **受控关键词 ID (`kw_id`)**：`KW-{HASH6}`（基于英文标准小写词条生成哈希）。

#### 1.2 SQLite 完整表结构 (`sqlite_manager.py`)
初始化 `workspace/atomic_meta.sqlite` 时，必须开启 `PRAGMA journal_mode=WAL;` 并创建以下数据表与 **FTS5 虚拟全文索引表**：

```sql
-- 1. 系统元数据与向量空间锁死校验表
CREATE TABLE IF NOT EXISTS system_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
-- 默认写入: ('embedding_signature', 'BAAI/bge-m3::dim=1024'), ('schema_version', '2.0')

-- 2. 元文件与下载/解析状态账本表 (Documents)
CREATE TABLE IF NOT EXISTS documents (
    doc_id TEXT PRIMARY KEY,              -- 如 DOC-PAP-2023-A9F21C
    doi TEXT UNIQUE,
    arxiv_id TEXT,
    title TEXT NOT NULL,
    authors TEXT,
    journal TEXT,
    pub_year INTEGER,
    doc_type TEXT DEFAULT 'PAP',          -- PAP / BOK / REV / MAN
    gatekeeper_score INTEGER DEFAULT 0,   -- 标题守门员评分 (0~100)
    authority_weight REAL DEFAULT 1.0,    -- 来源权威度权重 (专著/综述/CPC=1.5, 普通=1.0)
    download_status TEXT DEFAULT 'QUEUED',-- QUEUED / DOWNLOADED / MANUAL_REQUIRED / REJECTED
    parse_status TEXT DEFAULT 'PENDING',  -- PENDING / LIGHT_PARSED / LLM_EXTRACTED / FAILED
    source_url TEXT,
    local_pdf_path TEXT,
    local_md_path TEXT,
    error_log TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_docs_status ON documents(download_status, parse_status);

-- 3. L1/L2 元知识卡片主表 (Knowledge Cards)
CREATE TABLE IF NOT EXISTS knowledge_cards (
    knw_id TEXT PRIMARY KEY,              -- 如 KNW-STRAT-2023-8B4E11 或 MASTER-NELIKE-MCDHF
    card_level TEXT NOT NULL DEFAULT 'L2',-- 'L1' (顶层总纲) 或 'L2' (去重后的标准知识卡)
    card_type TEXT NOT NULL,              -- 'STRAT' / 'MODEL' / 'CODE' / 'PITFALL' / 'CONC'
    
    -- 四维物理坐标 (用于第 1 步毫秒级前置硬过滤)
    element TEXT,                         -- 如 'Fe'
    z_num INTEGER,                        -- 如 26
    ion_stage TEXT,                       -- 如 'Fe XVII'
    isoelectronic TEXT,                   -- 如 'Ne-like'
    shell_type TEXT,                      -- 如 'closed-shell', 'open-4f', 'alkali-like'
    method TEXT,                          -- 如 'MCDHF+RCI', 'RMBPT', 'RCC', 'CI+MBPT'
    code_name TEXT,                       -- 如 'GRASP2018', 'FAC', 'AMBiT', 'DIRAC'
    physical_process TEXT,                -- 如 'E1_transition', 'HFS', 'isotope_shift', 'energy_levels'
    
    -- 六维结论类型多标签位掩码 (Bitmask: 1=量化, 2=趋势, 4=机理, 8=存在, 16=边界局限, 32=建议决策)
    conclusion_flags INTEGER DEFAULT 0,
    
    -- 核心知识内容 (前提与结论强制绑定)
    title_zh TEXT NOT NULL,               -- 卡片中文简明标题
    premises_and_conditions TEXT NOT NULL,-- 【强制字段】完整前提条件与适用边界说明
    summary_zh TEXT NOT NULL,             -- 核心策略/模型/结论的中文精炼总结
    structured_details_json TEXT,         -- JSON格式详细参数 (如 MR参考集、活性空间 n/l、冻结芯层、近似层级等)
    formulas_latex TEXT,                  -- 关联的核心哈密顿量/势函数/矩阵元 LaTeX 公式
    code_snippet TEXT,                    -- 关联的程序工作流或输入文件脚本模板
    pitfall_warning TEXT,                 -- 踩坑预警与不收敛排错指南
    
    -- 检索拼接载荷 (专门用于喂给 BGE-M3 和 FTS5 的中英双语富上下文文本)
    search_payload TEXT NOT NULL,
    
    -- 共识度、权威度与冲突雷达标记
    consensus_count INTEGER DEFAULT 1,    -- 被多少篇独立文献共同证实
    authority_score REAL DEFAULT 1.0,
    recency_year INTEGER DEFAULT 2020,
    has_conflict INTEGER DEFAULT 0,       -- 0=正常, 1=发现极性相反的学术争议(⚡待慢脑裁决)
    conflict_with_knw_ids TEXT,           -- JSON 数组，记录与之冲突的卡片 ID
    supporting_doc_ids TEXT NOT NULL,     -- JSON 数组，如 ["DOC-PAP-2023-A9F21C", ...]
    is_vectorized INTEGER DEFAULT 0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_knw_coords ON knowledge_cards(card_level, card_type, isoelectronic, z_num, method, physical_process);

-- 4. SQLite FTS5 全文倒排索引虚拟表 (用于第 2 步混合检索的 A 路 BM25 召回)
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
    knw_id UNINDEXED,
    title_zh,
    search_payload,
    formulas_latex,
    code_snippet,
    tokenize='porter unicode61'
);

-- 5. L3 底层原始文献证据与表格块表 (Raw Evidence Chunks - 点击卡片展开时按需调取)
CREATE TABLE IF NOT EXISTS raw_evidence_chunks (
    chunk_id TEXT PRIMARY KEY,
    parent_knw_id TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    section_title TEXT,
    page_num INTEGER,
    context_bundle_md TEXT NOT NULL,      -- 三点式缝合后的原文证据包 (含前提+表格+高亮结论原句)
    FOREIGN KEY(parent_knw_id) REFERENCES knowledge_cards(knw_id),
    FOREIGN KEY(doc_id) REFERENCES documents(doc_id)
);

-- 6. 自举式动态受控关键词词表 (Controlled Vocabulary - 用于反哺长专著检索)
CREATE TABLE IF NOT EXISTS controlled_keywords (
    kw_id TEXT PRIMARY KEY,
    term_en TEXT UNIQUE NOT NULL,         -- 如 'core-valence correlation'
    term_zh TEXT,                         -- 如 '芯-价电子关联'
    synonyms_json TEXT,                   -- 如 '["CV correlation", "core polarization"]'
    category TEXT,                        -- 'CORRELATION' / 'BASIS' / 'QED_REL' / 'CONVERGENCE'
    hit_count INTEGER DEFAULT 1
);

-- 7. 原子物理符号标准化映射表 (Notation Normalization)
CREATE TABLE IF NOT EXISTS notation_map (
    raw_symbol TEXT PRIMARY KEY,          -- 如 '2p-', 'bar{2p}', 'Babushkin gauge'
    canonical_symbol TEXT NOT NULL,       -- 标准化为 '2p_{1/2}', '2p_{1/2}', 'Length gauge (Babushkin)'
    category TEXT                         -- 'ORBITAL' / 'COUPLING' / 'GAUGE'
);
```

---

### 模块 2：零成本 CPU 预处理、四通道探针与“三点式上下文缝合” (`app_core/parser/`)

这是确保**“既省去 80% 无关 Token，又绝不漏掉前提条件、公式、策略表格和代码”**的核心模块。

#### 2.1 下载前 3 秒守门员 (`crawler/title_gatekeeper.py`)
在调用下载器前，对文献元数据 `(title, abstract, keywords)` 执行正则打分：
* **一票否决（直接标记 `REJECTED`）**：命中纯实验装置、临床或纯天体观测词（`tokamak transport simulation`, `solar flare observation`, `Chandra X-ray spectrum`, `laser-produced plasma plume`）且未出现任何原子结构理论词（`MCDHF`, `RCI`, `RMBPT`, `coupled-cluster`, `Dirac-Fock`, `ab initio`, `B-spline`, `GRASP`, `FAC`, `AMBiT`, `CI+MBPT`）。
* **槽位加分**：命中计算方法（+40分）、命中目标物理过程（`transition`, `hyperfine`, `isotope shift`, `polarizability`, `energy levels` +30分）、命中策略关键词（`correlation`, `active space`, `convergence`, `Breit`, `QED` +30分）。得分 $\ge 40$ 分进入下载队列。

#### 2.2 四通道并行探针 (`parser/four_channel_radar.py`)
将 PDF 解析为按章节（Section/Subsection）组织的 Markdown 树后，并行运行以下 4 个探针：
1. **通道 A（结论与踩坑句探针 - 基于内置 `spaCy en_core_web_sm`）**：
   * 扫描图表引用紧邻句（`Table \d+` / `Fig. \d+` 当前句及后 2 句）；
   * 扫描包含认知/比较动词（`demonstrate, indicate, reduce, improve, converge, agree with, discrepancy, outperform`）且带有宾语从句（`ccomp`）的句子；
   * 扫描强转折与失效预警词（`However, Surprisingly, In contrast, fail to converge, variational collapse, intruder state`）；
   * **条件从句保护（Turn 6 规则）**：若句子含 `advcl` / `relcl`（如 `only when, if, provided that, whereas`），强制将主句与从句锁定为不可分割整体。
2. **通道 B（策略/收敛对比小表 vs. 生产数据大表分类器）**：
   * 提取所有 Markdown/HTML 表格：
   * 若表格行数 `< 50` 且表头/题注包含 `Model, Active space, CSF, VV, CV, CC, Convergence, Comparison, Reference` $\rightarrow$ 标记为 **`[💎 黄金策略表 (STRATEGY_TABLE)]`**，100% 保留！
   * 若表格行数 $\ge 50$ 且全是波长、振子强度罗列 $\rightarrow$ 标记为 **`[📦 海量数据表 (BULK_DATA_TABLE)]`**，从正文中剥离并单独存入本地文件，不送入 LLM。
3. **通道 C（哈密顿量、势函数与矩阵元公式探针）**：
   * 定位所有独立公式块 `$$...$$`，若前后 1 段内出现 `Hamiltonian, potential, operator, Breit, QED, polarization, matrix element, where ... is`，将 `[前1段 + 公式块 + 后1段变量释义]` 整体锁定。
4. **通道 D（程序工作流与代码参数探针）**：
   * 定位所有代码块或包含 `rcsfgenerate, rmcdhf, rci, jj2lsj, fac.Structure, B-spline, k=, R_max` 的段落。

#### 2.3 三点式上下文溯源缝合器 (`parser/context_bundler.py` —— 解决前提与结论分离问题！)
当四通道探针在某小节的第 $k$ 段命中目标时，**严禁只提取孤立单句**！程序必须自动组装以下三块内容生成 `ContextBundle`：
```python
class ContextBundle(BaseModel):
    doc_id: str
    section_path: str                     # 如 "Sec II.B > Relativistic CI Calculations"
    global_premise_paragraph: str         # 【拼图1】当前章/小节的第 1 段（交代算什么离子、用什么程序和全局近似）
    backtraced_definitions: List[str]     # 【拼图2】指代回溯：若目标段提到 "Model C" / "Set II" / "Table I" / "Eq. (3)"，
                                          # 自动往前扫描全文，将首次定义 "Model C" 的段落及 "Table I" 全表拼接到此！
    focal_window_markdown: str            # 【拼图3】目标句所在的完整自然段 + 前1段 + 后1段（目标结论句用 ⟪FOCUS⟫ 高亮）
    detected_channels: List[str]          # ['CONCLUSION', 'STRATEGY_TABLE', 'FORMULA', 'CODE', 'PITFALL']
    preliminary_tags_bitmask: int         # 由 conclusion_tagger.py 预打标的六维结论类型掩码
```

---

### 模块 3：统一大模型网关与结构化缝合 Prompt (`app_core/models/`)

#### 3.1 模型路由规范 (`gateway.py`)
* **内置轻量模型（纯本地 ONNX / spaCy，出厂自带或首次启动静默校验）**：
  * `spacy_en`: `en_core_web_sm`（句法分析与分句）
  * `mini_cluster_embed`: `all-MiniLM-L6-v2` (ONNX int8, 384维) —— 仅用于内存中英文原句的快速同义图聚类（Community Detection），不入主向量库。
  * `conflict_nli`: `nli-deberta-v3-xsmall` (ONNX int8) —— 用于校验两个同主题结论是否属于 `Contradiction`（极性冲突）。
  * `local_reranker`: `bge-reranker-base` 或 `ms-marco-MiniLM-L-6-v2` (ONNX int8) —— 本地重排器（支持在设置中切换为云端 Rerank API）。
* **全局唯一标准检索 Embedding（`BAAI/bge-m3`，1024 维）**：
  * 提供双通道无缝切换：`Cloud API (如硅基流动 SiliconFlow BAAI/bge-m3)` 与 `Local (Ollama bge-m3 或 ONNX int8 本地模型)`。两者共享同一 1024 维向量空间。
* **认知大模型（FastLLM & SlowReasoningLLM）与 VLM**：
  * 支持任意 OpenAI 兼容接口（DeepSeek-V3 / DeepSeek-R1 / OpenAI / Claude / Gemini / 本地 Ollama / vLLM）。

#### 3.2 快脑精读缝合 Prompt (`atomic_prompts.py`)
当 `FastLLM` 接收到模块 2 打包好的 `ContextBundle` 时，使用以下核心系统指令将其转化为自洽的 `knowledge_cards` 记录：
```text
你是一位顶尖的计算原子物理学家（精通 MCDHF, RCI, RMBPT, CC, CI+MBPT 及 GRASP/FAC/AMBiT/DIRAC 程序）。
现在给你一段从文献 [{doc_id}] 中通过“三点式上下文溯源”提取出的【富上下文证据包】（包含：小节全局前提、回溯的模型/表格定义、以及带有 ⟪FOCUS⟫ 标记的核心段落）。

请你完成以下【认知缝合任务】并输出严格的 JSON：
1. 消解所有代词与模型代号：如果结论中提到 "Model B"、"Set II" 或 "this scheme"，你必须结合【回溯定义】和【策略表格】将其替换为具体的物理操作（如：打开了哪一层芯壳层、激发阶数是 SD 还是 SDT、活性空间扩展到哪个 n/l 轨道）。
2. 强制绑定前提条件 (`premises_and_conditions`)：任何结论或计算策略都必须写明其生效的体系特征（如 Z 范围、壳层结构）、理论框架及前置近似，严禁输出脱离前提的孤立断言！
3. 提取数学公式与代码参数：若有哈密顿量、模型势函数（如芯极化势、QED 势），提取标准 LaTeX 并说明各符号物理意义与所做近似（如 NVPA、长波近似）。
4. 生成中英双语检索载荷 (`search_payload`)：按以下格式拼接，以供 BGE-M3 向量化：
   "[类型:{card_type} | 体系:{ion_stage} ({isoelectronic}, Z={z_num}) | 方法:{method} ({code_name}) | 过程:{physical_process}]
    [前提与边界]: {premises_and_conditions}
    [核心策略与结论(中)]: {summary_zh}
    [Keywords]: {提取出的英文受控专业术语列表}
    [Key Evidence(EN)]: {最关键的 1~2 句英文原句及关键量化指标}"
```

---

### 模块 4：三步高精检索引擎 (`app_core/db/hybrid_retriever.py`)

当用户在 GUI 搜索框或通过 MCP 提问时，严格按以下三步在 `< 50ms` 内返回结果：

1. **Step 1：查询意图解析与元数据前置硬过滤（Metadata Pre-Filtering）**
   * 用零延迟字典匹配用户提问中的元素/离子（如 `Fe XVII`, `类氖`, `Z=26`）、方法（`MCDHF`, `RMBPT`）、过程（`跃迁`, `HFS`）或直接输入的唯一 ID（`DOC-...`, `KNW-...`）。
   * 若匹配到唯一 ID，直接走主键毫秒级返回。
   * 若匹配到物理坐标，在 SQLite 和 Qdrant 中构造前置 Filter（仅在符合该物理坐标的 L1 总纲卡和 L2 知识卡子集中检索，直接排除 99% 无关数据）。
2. **Step 2：双路内容混合检索（Hybrid Content Search $\rightarrow$ Top 50）**
   * **A 路（字面精确匹配）**：在 `SQLite FTS5 (knowledge_fts)` 中对 `search_payload`, `formulas_latex`, `code_snippet` 执行 BM25 检索，取 Top 30。
   * **B 路（跨语言语义匹配）**：调用 `BGE-M3` 将用户提问编码为 1024 维向量，在 `Qdrant` 的 HNSW 图索引中检索 Top 30。
   * 使用 **RRF（倒数排名融合，$k=60$）** 合并 A、B 两路结果，去重后得到候选 **Top 50** 知识卡片。
3. **Step 3：Reranker 交叉精排 + 科学权威度加权（$\rightarrow$ Top 10）**
   * 将 `[用户提问, 候选卡片的 search_payload]` 成对送入 `Reranker` 计算相关性得分 $S_{\text{rerank}} \in [0, 1]$。
   * 计算最终综合排序分（打破同分僵局）：
     $$\text{FinalScore} = 0.65 \cdot S_{\text{rerank}} + 0.15 \cdot \min\left(1.0, \frac{\log_{10}(1 + N_{\text{consensus}})}{2}\right) + 0.12 \cdot W_{\text{authority}} + 0.08 \cdot W_{\text{recency}}$$
   * 返回得分最高的 **Top 10** 卡片（L1 总纲卡置顶高亮），并支持一键展开其关联的 L3 原始证据块（`raw_evidence_chunks`）与原 PDF 页码。

---

### 模块 5：夜间自动驾驶流水线与校园网瀑布流下载 (`crawler/` & `scheduler/`)

1. **4 级瀑布流下载器 (`waterfall_fetcher.py`)**：
   * **Level 1**：查 `arXiv API`（若有 `.tar.gz` LaTeX 源码包优先下载源码，其次 PDF）及 `Unpaywall` OA 链接。
   * **Level 2（单位/校园网 IP 直连）**：使用 `Playwright` 启动持久化 Chromium 上下文（复用本地校园网 IP 权限），访问 APS (`journals.aps.org`)、Elsevier (`sciencedirect.com`)、Springer、IoP 的 DOI 页面，自动捕获 PDF 流保存至 `workspace/storage/{doc_id}/`。
   * **Level 3（Sci-Hub 兜底）**：对 2021 年以前的未命中文献，轮询配置的 Sci-Hub 镜像站下载。
   * **Level 4（自动转入《待手动下载清单》）**：若前三级均失败，将 `download_status` 设为 `MANUAL_REQUIRED`。GUI 提供一键打开 DOI 链接按钮；当用户将手动下载的 PDF 拖入 GUI 或放入 `workspace/inbox_manual/` 时，`manual_inbox_watcher.py` 自动提取 PDF 首页 DOI/标题完成配对销账并继续解析。
2. **夜间睡眠蒸馏与慢脑会诊 (`night_worker.py`)**：
   * 支持设定每日运行时间窗（默认 `18:00 - 08:00`）。到达停止时间前 30 分钟停止拉取新文献，触发**“凌晨慢脑归纳阶段”**：
     1. 用内置 `MiniLM` + `NLI` 对当晚新入库的卡片与历史卡片做聚类比对：同义卡片直接合并（`consensus_count += 1`，不重复生成冗余向量）；极性相反的卡片标记 `has_conflict = 1`。
     2. 调用 `SlowReasoningLLM`（如 DeepSeek-R1）专门会诊所有 `has_conflict = 1` 的卡片对（对比双方的 `premises_and_conditions` 找出差异根源），并按等电子序列更新 `MASTER-*` L1 顶层总纲卡。
     3. 生成 `workspace/morning_reports/Report_YYYYMMDD.md` 简报并优雅休眠。

---

### 模块 6：Google Material Design 3 圆润风 GUI (`app_core/ui/`)

使用 `PySide6` 实现极简、温润的 Google Material You 界面：
1. **全局视觉规范 (`m3_theme.py`)**：
   * 主背景色 `#F8FAFD`，侧边导航栏 `#EFF3F8`，卡片纯白 `#FFFFFF` 配 `20px` 大圆角与柔和阴影，强调色谷歌蓝 `#0B57D0`（选中态胶囊底色 `#D3E3FD`）。
   * 顶部居中放置全圆角（`border-radius: 28px`）的超级搜索框；当用户输入时，搜索框下方实时浮现自动识别出的**四维物理坐标胶囊标签**与**六维结论类型过滤胶囊**（`📊量化` `📈趋势` `⚙️机理` `✅存在` `⚠️局限` `💡建议策略`）。
2. **左侧胶囊导航栏（Navigation Rail）五大视图**：
   * `🏠 探索中心`：搜索框 + L1 总纲横幅 + L2 知识卡片流（点击卡片右侧滑出抽屉展示公式渲染、代码高亮、L3 原文证据包与 PDF 原页定位）。
   * `🌙 夜间工作台`：定时启停开关、流水线实时进度看板、今日早报阅读器、以及【⚡学术争议与策略冲突】会诊专栏。
   * `📥 文献与待下清单`：引文滚雪球寻源发起器、已入库 `DOC-ID` 列表、**《需手动下载清单》（带一键跳转 DOI 与拖拽 PDF 销账区）**。
   * `☁️ 同步与 MCP`：`.acdb` 知识包一键导入/导出、WebDAV / S3 云同步配置、以及本地 `FastMCP Server` 状态开关（一键复制 Claude Desktop / Cursor 接入配置 JSON）。
   * `⚙️ 设置与热更新`：配置本地/云端 LLM、VLM、BGE-M3、Reranker 接口，内置 ONNX 模型状态检查，以及**【📦 导入更新包 (.zip) 立即热更新】**按钮。

---

## 四、 Agent 分步执行计划 (Step-by-Step Implementation Plan)

请 Agent 按照以下 5 个 Step 顺序编写代码，每完成一个 Step 进行一次模块级自检：

* **Step 1：创建基础框架、热更新壳程序 (`launcher.py`) 与数据库底座 (`app_core/db/`)**
  * 完成目录初始化、`launcher.py`、`updater/patch_manager.py`、`id_generator.py`、`sqlite_manager.py`（含全部表结构与 FTS5 触发器）及 `qdrant_manager.py`。
  * *自检标准*：运行测试脚本可成功创建 `workspace/atomic_meta.sqlite` 和 `workspace/vector_db/`，并能生成符合规范的 `DOC-ID` 与 `KNW-ID`。
* **Step 2：实现内置轻量模型管理与“四通道 + 三点式上下文缝合”解析器 (`app_core/parser/`)**
  * 完成 `model_registry.py`、`title_gatekeeper.py`、`pdf_to_markdown.py`、`four_channel_radar.py`、`context_bundler.py`、`conclusion_tagger.py` 和 `consensus_conflict_engine.py`。
  * *自检标准*：输入一段含表格、公式和跨段前提的测试文本，能准确输出包含完整前提与回溯表格的 `ContextBundle` 对象，并自动打上六维类型位掩码。
* **Step 3：实现统一 AI 模型网关、Prompt 缝合器与三步混合检索引擎 (`app_core/models/` & `hybrid_retriever.py`)**
  * 完成 `gateway.py`（支持 OpenAI 兼容 LLM/VLM 及本地/云端互通的 1024 维 `BGE-M3` 和 `Reranker`）、`atomic_prompts.py` 及 `hybrid_retriever.py`。
  * *自检标准*：可将 `ContextBundle` 转化为标准卡片写入 SQLite 与 Qdrant，并跑通“元数据过滤 $\rightarrow$ FTS5+Qdrant 混合召回 $\rightarrow$ Reranker+共识权威加权”全链路检索。
* **Step 4：实现校园网瀑布流爬虫、夜间调度器与同步/MCP 模块 (`crawler/`, `scheduler/`, `sync_mcp/`)**
  * 完成 4 级瀑布流下载器、手动拖拽监控器、夜间定时工作流、`.acdb` 导入导出、WebDAV/S3 同步及 `FastMCP` 服务端。
* **Step 5：构建 Google Material Design 3 圆润风 GUI 并串联全流程 (`app_core/ui/` & `main_entry.py`)**
  * 完成五大视图界面、QSS 样式表与后台工作线程信号槽绑定，确保界面流畅无卡顿，可通过 `python launcher.py` 一键启动整个系统。