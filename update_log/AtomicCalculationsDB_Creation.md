这份工程大纲专为 **`AtomicCalculationDB`** 量身定制。为了确保你能够**以最低的开发维护成本、最高的稳定性**把这套系统搭建出来（甚至可以直接把这份大纲喂给 Cursor / Windsurf 等 AI 编程工具逐模块生成代码），我将整个系统设计为**“纯本地文件化存储（免安装数据库服务端） + 壳核分离热更新架构（`launcher.py`）”**。

以下是 **`AtomicCalculationDB` 完整工程实现大纲**。

---

### 一、 总体技术栈选型（轻量化、免配置、易打包）

为了让你打包成 `.exe` 后在任何电脑上双击即用、且方便直接复制文件夹迁移，绝不使用需要后台安装服务的笨重数据库：

| 模块层级 | 推荐技术选型 | 选型理由（工程优势） |
| :--- | :--- | :--- |
| **开发语言** | `Python 3.11+` | AI 与科学计算生态最完善 |
| **GUI 框架** | `PySide6 (Qt6)` + **自定义 Material 3 (Google) QSS 主题** | 原生桌面性能，支持大圆角、柔和色调、流畅动画，与 `launcher.py` 完美配合 |
| **关系与全文检索库** | `SQLite3` + **内置 `FTS5` 扩展** | Python 原生自带，单文件存储；开启 `FTS5` 后直接具备毫秒级 **BM25 关键词倒排索引**能力 |
| **向量数据库** | `QdrantClient (Local Mode)` 或 `LanceDB` | **纯本地文件夹模式**（无需安装 Docker），所有向量直接存放在项目目录内，天然支持打包导出与云同步 |
| **爬虫与校园网下载** | `Playwright (Chromium)` + `httpx` | 直接复用你单位的校园网/研究所 IP 权限，穿透 APS/Elsevier/Springer 防火墙直接下载官方 PDF |
| **PDF 科学解析** | 本地 `MinerU` API / 云端或本地 `VLM` | 精准还原原子态符号（$^3P_1^o$）、矩阵元公式与跨页表格 |
| **云同步与外部协议** | `webdav4` (WebDAV) + `boto3` (S3) + `mcp` (`FastMCP`) | 标准工业级库，一键同步数据，并能把数据库变成 MCP Server 供 Claude/Cursor 直接调用 |
| **打包与热更新** | `PyInstaller (onedir)` + `launcher.py` 壳核分离 | 重型依赖编译进固定底座，业务代码外置，导入几 MB 的 `.zip` 更新包即可完成应用升级 |

---

### 二、 目录结构与 `launcher.py` 热更新架构（对应需求 8）

采用**“固定底座（Runtime Shell） + 外置核心（App Core）”**架构。首次用 PyInstaller 将所有第三方重型库（PySide6, Playwright, Qdrant, Boto3 等）与 `launcher.py` 打包成 `AtomicCalculationDB.exe`；而你所有的业务逻辑代码全部以源码或字节码形式放在外置的 `app_core/` 文件夹下。

```text
AtomicCalculationDB_Release/          # 最终发布的软件根目录
├── AtomicCalculationDB.exe           # 由 launcher.py 编译生成的启动器（永远不需要重新打包）
├── _internal/                        # PyInstaller 打包的 Python 运行时与所有第三方依赖库
│
├── app_core/                         # 【热更新区】所有业务代码放这里，通过导入更新包直接覆盖
│   ├── version.json                  # 记录当前版本号，如 {"version": "1.0.2", "build": "20261030"}
│   ├── main_entry.py                 # 真正的程序入口，由 launcher.py 动态加载
│   ├── ui/                           # Google Material 3 风格界面代码与圆润样式表 (m3_theme.qss)
│   ├── crawler/                      # 校园网 IP 爬虫 + arXiv + Sci-Hub + 待下载清单生成器
│   ├── parser/                       # PDF 目录路由、BM25 书本狙击器、MinerU/VLM 接口
│   ├── models/                       # 统一模型网关（本地 Ollama/vLLM/ONNX 与云端 API）
│   ├── db/                           # SQLite(FTS5) + Qdrant 本地向量库管理与双层 ID 引擎
│   ├── scheduler/                    # 夜间无人值守定时工作流引擎 (Night-Shift Worker)
│   ├── sync_mcp/                     # 导入导出、WebDAV、S3 同步与 FastMCP Server
│   └── updater/                      # 增量更新包校验与热替换模块
│
└── workspace/                        # 【全量数据区】所有知识库数据都在这，迁移/备份只需拷走此文件夹
    ├── config.yaml                   # 用户配置（API Key、夜间工作时间段、WebDAV/S3 账号）
    ├── atomic_meta.sqlite            # 核心关系库 + 任务状态账本 + FTS5 全文关键词索引
    ├── vector_db/                    # Qdrant 本地向量数据库持久化目录
    ├── inbox_manual/                 # 【手动导入监控区】用户手动下载的 PDF 拖进这里自动识别入库
    └── storage/                      # 元文件物理存储区（按唯一 DOC-ID 分文件夹存放 PDF、MD、脚本）
```

#### `launcher.py` 核心实现逻辑（极简伪代码）：
```python
import sys, os, zipfile, shutil

BASE_DIR = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, 'frozen', False) else __file__))
CORE_DIR = os.path.join(BASE_DIR, "app_core")
UPDATE_PKG = os.path.join(BASE_DIR, "workspace", "pending_update.zip")

def apply_update_if_exists():
    """启动时检查是否有用户在 GUI 中导入的更新包，若有则解压覆盖 app_core"""
    if os.path.exists(UPDATE_PKG):
        backup_dir = CORE_DIR + "_bak"
        if os.path.exists(backup_dir): shutil.rmtree(backup_dir)
        shutil.copytree(CORE_DIR, backup_dir)
        try:
            with zipfile.ZipFile(UPDATE_PKG, 'r') as zf:
                zf.extractall(CORE_DIR)
            os.remove(UPDATE_PKG)
        except Exception as e:
            # 更新失败自动回滚
            shutil.rmtree(CORE_DIR)
            shutil.move(backup_dir, CORE_DIR)

if __name__ == "__main__":
    apply_update_if_exists()
    sys.path.insert(0, CORE_DIR)
    from main_entry import start_gui_app
    start_gui_app(BASE_DIR)
```
* **程序内更新操作**：你在 GUI 菜单里点击 `导入更新包 (.zip)` $\rightarrow$ 程序校验压缩包内含合法的 `version.json` $\rightarrow$ 将其复制到 `workspace/pending_update.zip` $\rightarrow$ 自动重启 `AtomicCalculationDB.exe`，1 秒钟完成增量更新！

---

### 三、 八大核心功能模块详细工程设计

#### 模块 1：校园网 IP 优先的多级爬虫与手动补全闭环（对应需求 1）
利用你单位 IP 可免费下载 APS (PRA)、Springer、Elsevier (ADNDT, CPC)、IoP (J. Phys. B) 的天然优势，设计 **4 级瀑布流下载器 (`crawler/fetcher.py`)**：
1. **Level 1（源码与开源优先）**：查询 `arXiv API` 与 `Unpaywall API`。若存在 arXiv 版本，优先下载 `.tar.gz` LaTeX 源码包（公式 100% 准确免 OCR）及 PDF。
2. **Level 2（单位 IP 浏览器直连）**：针对 APS / Elsevier / Springer / IoP 的 DOI，启动内置的 **Playwright 无头浏览器（复用本地网络环境与 Cookie）**，访问 `https://doi.org/{DOI}`，自动识别出版商页面的 `Download PDF` 按钮并抓取官方高清 PDF。
3. **Level 3（Sci-Hub 兜底）**：若前两步因年代久远或未订阅而失败，自动轮询配置好的 Sci-Hub 镜像站域名，通过 DOM 解析提取 PDF 直链下载。
4. **Level 4（生成《待手动下载清单》与拖拽入库）**：
   * 若前三级全败，将该文献状态在 SQLite 中标记为 `MANUAL_REQUIRED`。
   * 在 GUI 的【待办清单】页面生成一张交互式表格（也可一键导出为 `待下载清单.html`），包含：**文献标题、作者、期刊、年份、可点击跳转的 DOI 链接、分配好的唯一文件 ID（如 `DOC-PAP-2024-8F3A`）**。
   * **手动导入极简体验**：用户用浏览器下好 PDF 后，**直接拖入软件界面**（或丢进 `workspace/inbox_manual/` 文件夹），系统自动读取 PDF 首页提取 DOI 或标题，自动与待下载清单中的 `DOC-ID` 配对销账，并无缝排入今晚的解析队列！

#### 模块 2：统一大模型网关（对应需求 2）
在 `models/gateway.py` 中使用适配器模式（Adapter Pattern），所有配置在 GUI 设置页中自由切换：
* **LLM 接口**：统一封装为 OpenAI 兼容格式。支持本地（`Ollama`, `vLLM`, `LM Studio`）与云端（`DeepSeek`, `OpenAI`, `Claude`, `Gemini`, `硅基流动 SiliconFlow`）。支持分別指定**“快脑（日常提取）”**和**“慢脑（深度推理总结）”**。
* **VLM 接口**：支持本地（`MinerU` 本地服务、`Ollama Qwen2.5-VL`）与云端多模态 API（`Qwen-VL-Max`, `GPT-4o`, `Gemini-1.5-Pro`），用于攻克扫描版老专著或复杂图表。
* **Embedding 接口**：支持本地加载（通过 `FastEmbed` / `Sentence-Transformers` 运行 `BAAI/bge-m3`，支持稠密+稀疏向量）与云端 Embedding API。
* **Rerank 接口**：支持本地加载（`bge-reranker-v2-m3` ONNX 加速版）与云端 Rerank API（硅基流动 / Jina / Cohere）。

#### 模块 3 & 4：双层唯一 ID 体系、元知识提取与检索（对应需求 3、4）
所有进入系统的数据都赋予**人类可读且全局唯一的定长 ID**，支持在搜索框直接输入 ID 精确调取，也支持点击关联 ID 在卡片间跳转：

1. **元文件 ID（Meta-File ID）**：代表物理文献或书籍本身
   * 格式：`DOC-{类型}-{年份}-{6位哈希}`
   * 示例：`DOC-PAP-2023-A9F21C`（论文）、`DOC-BOK-2007-GRANT1`（Grant 专著）、`DOC-MAN-2018-GRASP0`（程序手册）。
2. **元知识 ID（Meta-Knowledge ID）**：从元文件中提炼出的结构化知识节点（每个节点均绑定其来源的 `DOC-ID` 及具体页码/章节号）
   * `KNW-STRAT-xxxx`：**【计算策略卡】**（目标体系如 $\text{Fe XVII}$、物理过程、选用的方法、参考组态集 MR、活性空间逐层扩展方案、VV/CV/CC 关联截断、基组参数）。
   * `KNW-MODEL-xxxx`：**【物理模型卡】**（方法名称、历史演化脉络、哈密顿量与势函数 LaTeX 表达式、采用的近似清单如 NVPA、优劣势与适用边界）。
   * `KNW-CODE-xxxx`：**【程序与代码卡】**（程序名如 GRASP/FAC/AMBiT、标准执行工作流、核心控制参数字典、真实输入脚本范例）。
   * `KNW-CONC-xxxx`：**【关键结论与排错卡】**（核心物理结论、不确定度评估方法如长度/速度规范对比、变分崩塌/闯入态等踩坑排错记录）。
   * `KW-xxxx`：**【受控关键词词条】**（英文标准术语、中文释义、同义词环如 `CV correlation = core-valence = 芯价关联`、关联的元知识 ID 列表）。
3. **双模检索引擎**：
   * **按 ID 搜索**：输入 `DOC-...` 或 `KNW-...` 直接毫秒级打开对应详情页及其上下游引用关系。
   * **按内容搜索**：`SQLite FTS5 (BM25 关键词检索)` + `Qdrant (语义向量检索)` 双路召回 Top 50 $\rightarrow$ 经由 `Reranker 重排模型` 精排至 Top 10 $\rightarrow$ 展示匹配的元知识卡片与原文高亮切片。

#### 模块 5：长文献与厚重专著快速定位引擎（对应需求 6）
在 `parser/long_doc_locator.py` 中实现**“三级漏斗定位法”**，绝不盲目喂全书：
1. **目录与书末索引路由（ToC & Index Router）**：自动截取书籍前 15 页（目录）与最后 20 页（Index 术语表），交由 LLM 识别出与“数值方法、电子关联、基组、收敛策略、模型势”相关的精确页码区间，直接跳过纯角动量代数推导章节。
2. **动态关键词倒排狙击（Keyword Sniper）**：利用已入库的 `KW-xxxx`（动态积累的计算策略关键词表），在书籍的 `SQLite FTS5` 全文索引中执行毫秒级打靶，命中后仅截取目标段落及其前后各 2 段（滑动窗口上下文）。
3. **策略浓度打分（Density Filter）**：对截取出的小节计算“策略特征词密度”，仅对高浓度段落调用大模型进行深度提取并入库。

#### 模块 6：全量导入导出、迁移与多端云同步（对应需求 5）
在 `sync_mcp/` 模块下实现全方位的数据流动：
1. **导入与导出（`.acdb` 标准包）**：
   * 支持将全部数据、或按条件筛选出的某类知识（如“所有关于 RMBPT 的模型与策略”），一键导出为 `.acdb` 文件（本质是内含 `manifest.json`、`meta_dump.jsonl`、`vectors.parquet` 及可选原文 PDF 的压缩包）。
   * 导入时基于唯一 ID（`DOC-ID` / `KNW-ID`）进行哈希比对：**相同 ID 自动增量合并更新，新增 ID 直接并入**，方便课题组同事之间互相分享知识包。
2. **WebDAV 与 S3 云同步**：
   * **WebDAV**：内置 `webdav4` 客户端，支持坚果云、Nextcloud、群晖 NAS 等。
   * **S3 协议**：内置 `boto3` 客户端，支持 AWS S3、Cloudflare R2、阿里云 OSS、MinIO。
   * **增量同步机制**：维护一张本地文件哈希表，每天早上夜间任务结束后，仅将发生变动的 SQLite 快照、新增的向量分片与新增的 `DOC-ID` 文献包增量上传至云端。
3. **MCP (Model Context Protocol) 服务端集成**：
   * 使用官方 `mcp` Python SDK (`FastMCP`) 在软件内部集成一个标准 MCP Server（支持 `SSE` 本地端口监听和 `stdio` 两种模式）。
   * 暴露标准工具集（Tools）：
     * `query_calculation_strategy(element_or_ion, process, method)`
     * `explain_atomic_model(model_name, aspect)`
     * `lookup_by_id(entity_id)`
     * `get_code_template(program_name, task_type)`
   * **效果**：你在 Cursor、Claude Desktop 或 Cherry Studio 里写论文或写计算脚本时，外部 AI 可以直接通过 MCP 协议实时查阅你本地 `AtomicCalculationDB` 中的全部文献结晶！

---

### 四、 GUI 界面设计规范：Google Material Design 3 简约圆润风（对应需求 7）

采用 **Google Material You (Material Design 3)** 设计语言，通过 `PySide6` 无边框窗口（Frameless Window）配合自绘圆角与全局 QSS 样式表实现：

#### 1. 视觉规范参数（Design Tokens）
* **圆角（Border Radius）**：主窗口圆角 `16px`；内容卡片圆角 `20px`；搜索框与胶囊按钮采用全圆角 `999px`（Pill Shape）。
* **色彩体系（Google Tonal Palette）**：
  * 主背景（Surface）：柔和极浅蓝灰 `#F8FAFD`（告别刺眼纯白）。
  * 侧边导航栏与次级容器（Surface Container）：`#EFF3F8`。
  * 知识卡片背景：`#FFFFFF`，配极柔和弥散阴影（`0 4px 16px rgba(0,0,0,0.04)`），无生硬黑色边框。
  * 强调色（Primary Accent）：谷歌蓝 `#0B57D0`，选中态胶囊底色 `#D3E3FD`。
* **字体排版**：英文与数字采用 `Google Sans` / `Inter`，中文采用 `HarmonyOS Sans SC` 或 `微软雅黑`，公式区采用 KaTeX / MathJax 渲染组件。

#### 2. 五大核心视图布局（左侧胶囊导航栏 Navigation Rail）
```text
+-----------------------------------------------------------------------------------+
|  ● ● ●   AtomicCalculationDB               [ 🔍 输入内容或唯一 ID (如 KNW-...)   ] |
+--------+--------------------------------------------------------------------------+
|        |                                                                          |
| ( 🏠 ) |  【当前视图：知识探索与策略中心】                                        |
|  探索  |  +----------------------------------+  +-------------------------------+ |
|        |  | 🏷️ KNW-STRAT-0842  [MCDHF+RCI]  |  | 🏷️ KNW-MODEL-0019  [RMBPT]    | |
| ( 🌙 ) |  | 体系: Fe XVII (Ne-like)          |  | 模型: 相对论多体微扰理论      | |
| 夜间工 |  | 策略: 2s/2p 芯层打开，CV关联至n=7|  | 势函数: Dirac-Coulomb-Breit   | |
|  作台  |  | 来源: [DOC-PAP-2022-9A1B] (P.4)  |  | 近似: NVPA + 二阶截断         | |
|        |  +----------------------------------+  +-------------------------------+ |
| ( 📥 ) |                                                                          |
| 文献与 |  +---------------------------------------------------------------------+ |
| 待下表 |  | 🌙 夜间自动驾驶状态：已就绪 (今晚 18:00 自动启动 -> 明早 08:00 停止)| |
|        |  | 待处理队列: 142 篇论文 | 1 本专著 (Grant)  [ 立即启动 ] [ 查看早报 ]| |
| ( ☁️ ) |  +---------------------------------------------------------------------+ |
| 同步与 |                                                                          |
|  MCP   |  +---------------------------------------------------------------------+ |
|        |  | 🔥 动态关键词云 (点击反哺检索专著):                                 | |
| ( ⚙️ ) |  | ( core-valence ) ( Breit interaction ) ( kinetic balance ) ( EOL )  | |
|  设置  |  +---------------------------------------------------------------------+ |
+--------+--------------------------------------------------------------------------+
```
* **【探索中心】**：顶部是谷歌风格的超大胶囊搜索框（支持自然语言提问、关键词检索、ID 精确直达），下方以圆角卡片瀑布流展示【计算策略卡】、【物理模型卡】、【代码模板】与【排错经验】，点击卡片上的 `DOC-ID` 可在右侧抽屉栏直接预览原 PDF 对应页。
* **【夜间工作台】**：设置每日自动运行时间窗（如 `18:00 - 08:00`），实时显示当前流水线进度条（寻源 $\rightarrow$ 下载 $\rightarrow$ 解析 $\rightarrow$ 提取 $\rightarrow$ 书本反哺 $\rightarrow$ 慢脑归纳），以及查阅每天早上生成的《夜间学习简报》。
* **【文献库与待下载清单】**：管理所有 `DOC-ID` 元文件，提供醒目的【需手动下载列表（一键点击跳转 DOI）】和【拖拽 PDF 入库区】。
* **【同步与 MCP】**：一键导入/导出 `.acdb` 数据包，配置 WebDAV / S3 自动同步，以及一键开启本地 MCP Server（自动生成可粘贴到 Claude/Cursor 的 JSON 配置代码）。
* **【设置与热更新】**：配置本地/云端 VLM、LLM、Embedding、Reranker 地址与测试连接，以及点击【导入更新包 (.zip)】实现一键增量升级。

---

### 五、 分阶段开发落地路线图（建议按此顺序实现）

为了让你最快看到成果并投入使用，建议分 4 个里程碑（Milestone）推进：

* **Milestone 1（第 1 周）：地基与壳核框架搭建**
  * 编写 `launcher.py` 和基础的 `PySide6` Google Material 3 主窗口骨架。
  * 建立 `SQLite (含 FTS5)` 数据表结构（定义 `docs` 表、`knowledge_nodes` 表、`keywords` 表）与 `Qdrant` 本地文件夹初始化，跑通唯一 ID 生成器与按 ID/内容搜索。
* **Milestone 2（第 2 周）：模型网关与单篇文献精读流水线**
  * 接入统一模型网关（配置好你的 LLM、VLM/MinerU、Embedding、Reranker）。
  * 实现“拖入一篇 PDF 或输入一个 DOI $\rightarrow$ 校园网 Playwright / Sci-Hub 自动下载 $\rightarrow$ 解析出【策略卡】【模型卡】【代码卡】【关键词】并入库”的完整单点闭环。
* **Milestone 3（第 3 周）：长书定位引擎与夜间无人值守调度器**
  * 实现长专著的“目录路由 + 关键词倒排狙击”功能。
  * 加入定时任务调度器（`18:00 - 08:00` 循环消费队列、失败自动转入《待下载清单》、天亮生成简报并执行慢脑跨文献总结）。
* **Milestone 4（第 4 周）：导入导出、WebDAV/S3 云同步与 MCP 赋能**
  * 加上 `.acdb` 打包导入导出、WebDAV/S3 同步按钮，并用 `FastMCP` 暴露出查询接口。最后用 PyInstaller 将 `launcher.py` 打包成 `.exe`，正式投入每晚的自动化运行！

如果你准备开始写代码，我们可以先从 **Milestone 1 的数据库表结构设计（SQLite Schema）与 `launcher.py` 完整代码**，或者 **提取原子物理策略与模型的专用 Prompt 模板** 开始，随时告诉我你想先展开哪一块！