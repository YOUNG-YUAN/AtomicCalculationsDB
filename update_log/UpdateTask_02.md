# UpdateTask_02.md — `AtomicCalculationDB` (V2.1) 品牌图标、独立 EXE 热更新、全时段工作台与满血消缺任务书

> **致 AI Coding Agent 的全局执行说明**：
> 在 V2.0 的基础上，本次 V2.1 升级旨在将项目打磨为**零妥协的桌面级成品软件**。请严格按以下 6 项核心任务完成代码升级、解决 V2.0 遗留的 5 项“已知边界”、集成根目录下的 `LOGO-AtomicCalculationsDB.svg` 品牌图标，并编译产出支持运行中增量热更新的 `AtomicCalculationDB.exe`。

---

## Task 1：集成品牌图标 (`LOGO-AtomicCalculationsDB.svg`)

用户已在项目根目录下放置了定稿的矢量图标文件：**`LOGO-AtomicCalculationsDB.svg`**。

1. **自动转换与多尺寸资源生成 (`app_core/ui/icon_assets.py`)**：
   * 编写资源构建逻辑（利用已安装的 `PyMuPDF (fitz)` 或 `PySide6.QtSvg.QSvgRenderer`），将根目录下的 `LOGO-AtomicCalculationsDB.svg` 渲染生成：
     * `app_core/ui/assets/logo.svg`（原文件副本，供 GUI 矢量缩放使用）
     * `app_core/ui/assets/logo_256.png`、`logo_64.png`、`logo_32.png`
     * `app_core/ui/assets/logo.ico`（包含 `16x16, 32x32, 48x48, 64x64, 128x128, 256x256` 多图层 Windows 标准图标，供 `.exe` 编译使用）。
2. **全界面图标绑定**：
   * 将 `QApplication.setWindowIcon` 与主窗口左上角品牌栏图标、系统托盘图标（`QSystemTrayIcon`）、探索中心空状态/欢迎横幅全部绑定为该新 Logo。
   * 在 Windows 下调用 `ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("AtomicCalculationDB.App.V2")` 确保 Windows 任务栏正确显示自定义 Logo 图标而非默认 Python 图标。

---

## Task 2：打包独立 `AtomicCalculationDB.exe` 与运行中 GUI 一键增量更新

改变必须用命令行 `python launcher.py` 启动的方式，构建可直接双击运行的 **`AtomicCalculationDB.exe`**，并实现真正的**“壳核分离 + 程序运行中导入更新包自动升级重启”**。

1. **壳核分离打包配置 (`build_exe.py` & `AtomicCalculationDB.spec`)**：
   * 使用 `PyInstaller` 将 `launcher.py` 编译为 **`AtomicCalculationDB.exe`**（绑定 `--icon=app_core/ui/assets/logo.ico` 且使用 `--noconsole` / `--windowed` 模式，但当传入 `--mcp` 或 `--mcp-sse` 命令行参数时正常接管标准输入输出）。
   * **关键架构要求**：所有第三方依赖库打包进运行底座，而 **`app_core/` 目录必须作为外部独立文件夹存在（不被锁死在 `.exe` 内部的 `PYZ` 归档中）**，确保 `launcher.py` 启动时始终优先从磁盘外部的 `./app_core/` 加载业务代码！
2. **GUI 运行中一键导入更新包与自动热重启 (`app_core/updater/patch_manager.py` & GUI)**：
   * 在 GUI 顶部状态栏右侧及【⚙️ 设置与热更新】页面顶部，放置醒目的 Google Material 3 胶囊按钮：**`[📦 导入更新包 (.zip) 立即更新]`**，并显示当前版本号（如 `v2.1.0`）。
   * **运行中热更新闭环流程**：
     1. 用户在程序运行中点击按钮 $\rightarrow$ 弹出文件选择框选择 `.zip` 更新包（支持包内直接是 `version.json + 各模块目录` 或包含一层 `app_core/` 根目录）。
     2. 校验压缩包合法性并读取新版本号后，将其复制暂存为 `workspace/pending_update.zip`。
     3. 弹出圆润风确认提示框（或倒计时 2 秒自动执行）：*“更新包 (vX.X.X) 已校验就绪，正在自动重启并应用更新...”*。
     4. 安全关闭当前 SQLite 与 Qdrant 连接（释放文件锁），调用 `QProcess.startDetached(sys.executable, sys.argv)` 启动新进程并立即退出当前进程（`QApplication.quit()`）。
     5. 新启动的 `AtomicCalculationDB.exe` (`launcher.py`) 检测到 `workspace/pending_update.zip` $\rightarrow$ 自动将旧 `app_core/` 备份至 `app_core_bak/` $\rightarrow$ 解压覆盖 `app_core/` $\rightarrow$ 若启动异常则自动回滚 `app_core_bak/` $\rightarrow$ 拉起更新后的新版 GUI！
   * 同时提供一个辅助脚本 `scripts/make_update_patch.py`，可一键将当前最新的 `app_core/` 打包生成标准的 `AtomicCalculationDB_Patch_v2.1.x.zip` 供测试与分发。

---

## Task 3：“夜间工作台”更名为全时段“工作台” + 更换图标 + 任意时间段定时启停

用户不仅在夜间运行，也可能在白天（如午休 `12:00-14:00`、下午开会 `14:30-17:30` 或跨夜 `18:00-08:00`）定时挂机运行。

1. **全面更名与话术升级**：
   * 将左侧导航栏、视图标题、状态栏中的 **“夜间工作台”** 统一更名为 **“工作台”**（副标题：`全自动文献挖掘与知识蒸馏中心`）。
   * 将原有的“早安简报 (`morning_reports`)”在界面上更名为 **“工作台运行简报”**（保留底层目录向后兼容）。
2. **更换导航栏“月亮”图标**：
   * 彻底移除原有的月亮图标（`🌙`）。
   * 在左侧胶囊导航栏（Navigation Rail）及工作台页面中，替换为一枚契合 Google Material 3 圆润风格的**【⚡ 自动化反应堆 / 智能脉冲引擎】矢量 SVG 图标**（外圈为带箭头的圆润循环轨迹，中心为闪电/脉冲核心，代表全自动高速运转）。
3. **支持任意时间段的定时启停调度器 (`app_core/scheduler/night_worker.py`)**：
   * 在【工作台】界面顶部卡片中，提供直观的 **定时运行时间段设置组件**：
     * **`[ 启用定时自动运行 ]` 开关**
     * **开始时间 (`start_time`, `HH:MM`)** 与 **结束时间 (`end_time`, `HH:MM`)** 选择器（支持快捷预设胶囊：`午休快跑 12:00-14:00`、`下午时段 14:00-18:00`、`夜间挂机 18:00-08:00`、`全天候持续运行`）。
     * **`[ ▶️ 立即启动运行 ]`** 与 **`[ ⏹️ 优雅停止并生成简报 ]`** 手动控制按钮。
   * **跨天与非跨天时间窗精确判定逻辑**：
     * 当 `start_time < end_time`（同日时间段，如 `13:00` 至 `17:00`）：当前时间满足 `start_time <= now < end_time` 时处于运行窗口。
     * 当 `start_time > end_time`（跨午夜时间段，如 `18:00` 至次日 `08:00`）：当前时间满足 `now >= start_time` 或 `now < end_time` 时处于运行窗口。
     * 当 `start_time == end_time`（全天候模式）：只要定时开关开启即持续运行。
   * **自动触发与收尾机制**：
     * 后台守护定时器每 20 秒检查一次当前时间：一旦进入设定时间段且队列有任务，自动唤醒流水线开始工作；
     * 在距离 `end_time` 剩余 15 分钟（或到达 `end_time`）时，自动停止拉取新文献，触发“慢脑跨文献冲突会诊与 L1 总纲更新”，生成本次《工作台运行简报》后优雅休眠。

---

## Task 4：彻底消除 V2.0 的 5 项“已知边界”（满血激活）

### 4.1 消除边界 1：内置轻量 ONNX 模型真实下载器与状态看板 (`model_registry.py`)
* **问题**：V2.0 仅编写了降级回退逻辑，未提供获取真实 ONNX 模型权重的内置通道。
* **修复要求**：
  1. 在 `app_core/builtin_models/model_registry.py` 中实现支持国内高速镜像（优先使用 `https://hf-mirror.com`，失败自动回退官方源）的 **ONNX 模型自动拉取器（带进度回调与 SHA256/文件完整性校验）**，支持下载并加载：
     * `all-MiniLM-L6-v2` (ONNX int8 + `tokenizer.json`) —— 用于快速同义图聚类
     * `nli-deberta-v3-xsmall` (ONNX int8 + `tokenizer.json`) —— 用于极性冲突与学术争议 NLI 校验
     * `ms-marco-MiniLM-L-6-v2` 或 `bge-reranker-base` (ONNX int8 + `tokenizer.json`) —— 用于本地 Top 50 $\to$ Top 10 交叉重排
  2. 在 GUI【⚙️ 设置与热更新】页面新增 **“🧠 本地轻量神经网络引擎 (ONNX)”** 专属卡片，展示每个模型的实时状态徽章（`🟢 ONNX 已激活` vs `🟡 启发式降级中`），并提供 **`[⬇️ 一键下载并激活全部内置 ONNX 模型]`** 按钮（带实时下载进度条）以及 **`[📂 从本地文件夹导入模型]`** 按钮。

### 4.2 消除边界 2：内置高清物理公式渲染 (`requirements.txt` & 公式组件)
* **问题**：`matplotlib` 未放入基础依赖，且复杂物理公式易退化为源码。
* **修复要求**：
  1. 将 `matplotlib>=3.8.0` 正式加入基础 `requirements.txt` 并安装到位。
  2. 升级公式渲染器 (`app_core/ui/formula_renderer.py`)：对原子物理常见的 LaTeX 宏（如 `\bra{...}`, `\ket{...}`, `\braket{...}`, `\hat{H}`, `\mathrm{...}`, `\text{...}`, 多行 `\begin{aligned}`）做预展开转换，使用 `matplotlib.mathtext` 渲染为高 DPI 透明底色 PNG 嵌入 GUI 卡片与右侧证据抽屉中；同时在公式下方提供一键复制原始 LaTeX 代码的微交互按钮。

### 4.3 消除边界 3：WebDAV / S3 云同步拉取后自动增量热合并 (`cloud_sync.py`)
* **问题**：从 WebDAV/S3 拉取的数据仅暂存在 `workspace/sync_incoming/`，未自动并入当前在线数据库。
* **修复要求**：
  1. 当 `cloud_sync.pull()` 将云端快照或同步包下载至 `workspace/sync_incoming/` 后，立即自动调用 `acdb_packager` 的**按唯一 ID (`doc_id` / `knw_id` / `kw_id`) 增量热合并引擎**：
     * 自动对比 `sync_incoming/` 中的记录与当前在线 SQLite / Qdrant 库：新增的 `DOC-ID`、`KNW-ID`、证据块与关键词直接热写入当前在线库并同步更新向量索引；已存在的卡片若云端 `updated_at` 更新或 `consensus_count` 更高，则自动合并更新！
  2. 合并完成后自动清理 `sync_incoming/` 临时文件，并在 GUI 弹出通知：*“云端同步完成：新增 X 篇文献、Y 张知识卡，更新 Z 张卡片”*，立即刷新界面视图。

### 4.4 消除边界 4：彻底解决 GUI 运行时与外部 MCP 进程的 Qdrant 本地锁冲突 (`mcp_server.py`)
* **问题**：Qdrant Local Mode 独占锁导致 GUI 开启时，外部启动的 `--mcp` 进程无法访问向量库而降级为纯 FTS。
* **修复要求（内存共享代理架构）**：
  1. 当 GUI 主程序启动时，在后台守护线程中自动启动一个轻量级本地环回 IPC/HTTP 检索代理服务（默认监听 `127.0.0.1:8765`，若端口占用则递增并写入 `workspace/runtime_bridge.json`），该服务直接复用 GUI 进程内存中已打开的唯一 `HybridRetriever (SQLite + QdrantClient)` 实例！
  2. 当外部 AI 客户端（如 Claude Desktop / Cursor）通过命令行启动 `AtomicCalculationDB.exe --mcp`（或 `python launcher.py --mcp` stdio 模式）时：
     * `--mcp` 进程首先检查 `workspace/runtime_bridge.json` 中的本地代理端口是否存活；
     * **若 GUI 正在运行（代理存活）**：`--mcp` 进程不直接打开 `workspace/vector_db/`（从而彻底规避文件锁冲突！），而是将所有 MCP 工具请求通过本地环回接口无缝转发给 GUI 内存中的满血 `HybridRetriever` 执行，返回 100% 包含 `BGE-M3` 向量召回 + `Reranker` 重排的高精结果！
     * **若 GUI 未运行**：`--mcp` 进程才直接以独立模式打开 `SQLite + Qdrant` 执行满血混合检索。

### 4.5 消除边界 5：GUI 显式配置文献兜底镜像源与连通性测试 (`views/settings_view.py`)
* **修复要求**：
  * 在 GUI【⚙️ 设置与热更新】页面中，增加 **“🌐 文献四级瀑布流下载与兜底源配置”** 卡片：
    * 提供 `[启用校园网/机构 IP 出版商直连 (Level 2)]` 开关；
    * 提供 `[启用自定义兜底镜像源 (Level 3)]` 开关，并配有**镜像域名列表输入框**（支持用户自行填入如 Sci-Hub 或机构代理域名，每行一个，实时保存至 `workspace/config.yaml`）及 **`[⚡ 测试镜像连通性]`** 按钮。

---

## 五、 Agent 交付与验证清单 (Verification Checklist)

请 Agent 按顺序完成上述 4 大 Task 后，执行以下验证并汇报结果：

1. **图标与新工作台 UI 验证**：
   * 确认 `LOGO-AtomicCalculationsDB.svg` 已生成 `.ico` 与 `.png` 并应用到窗口、导航栏与探索中心。
   * 确认左侧导航栏已更名为 **“工作台”**（新自动化矢量图标），且工作台内可自由设置任意时间段（同日或跨天）并正常触发定时运行。
2. **满血消缺单元测试 (`pytest`)**：
   * 新增并通过针对 **任意时间段判定（同日/跨夜）**、**WebDAV/S3 拉取后自动热合并**、**GUI+MCP 双进程并发满血向量检索（零 Qdrant 锁冲突）**、**公式高清渲染** 以及 **ONNX 模型下载/加载器** 的自动化测试。
3. **编译产出 `AtomicCalculationDB.exe` 与运行中热更新实测**：
   * 运行打包脚本生成带自定义图标的 `AtomicCalculationDB.exe`（确保 `app_core/` 外置可热替换）。
   * 实测通过 GUI 中的 `[📦 导入更新包 (.zip) 立即更新]` 按钮，验证在程序运行状态下导入补丁包并自动完成升级重启。
4. **更新截图**：
   * 重新截取并更新 `docs/` 下的真实界面截图（包含带有新 Logo、新“工作台”界面、ONNX 模型管理卡片与热更新按钮的最新状态）。