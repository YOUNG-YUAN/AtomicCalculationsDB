# FixTask_04_04.md — `AtomicCalculationDB` (V2.3.5) Google AI Studio 专项适配（常驻模型配置与反拦截登录）及算力节点弹窗动态表单净化任务书

> **致 AI Coding Agent 的全局执行说明**：
> 在 V2.3 的算力节点配置与网页版 AI (Playwright) 实测中发现两个核心问题：
> 1. **编辑算力节点弹窗“一锅炖”**：无论“节点类型”选什么，所有的输入框（Base URL、本地 ONNX 路径、API Key、网页地址、排队 URL）全部堆在一起显示，极其容易让用户填错，且弹窗内缺少直接启动浏览器登录/预设模型的按钮。
> 2. **调用 Google AI Studio (`https://aistudio.google.com`) 等高级网页端存在三大障碍**：
>    * 默认 Chromium 会被 Google 账号拦截提示“此浏览器或应用可能不安全”；
>    * 用户需要在网页右侧栏手动选定特定模型（如 `Gemini 4 argon`）及思考强度（`High`），若每次请求都重新 `goto` 刷新页面会导致用户选好的模型与参数被重置；
>    * Google AI Studio 的发送触发键是 `Ctrl + Enter`（或点击 `Run` 按钮），而非普通回车 `Enter`。
> 请严格按以下 3 项任务完成修复并更新 `AtomicCalculationDB.exe`。

---

## Task 1：净化“编辑算力节点”弹窗 UI —— 根据【节点类型】动态显示/隐藏表单项 (`views/settings_view.py`)

严禁在编辑节点弹窗中同时显示所有无关字段！必须监听 `节点类型 (node_type)` 下拉框的切换事件，**动态显示当前类型所需的字段，彻底隐藏无关字段**：

1. **当选择 `🌐 网页版 AI (Playwright)` 时**：
   * **仅显示**：
     * `显示名称`（如：`Google AI Studio - Gemini 4 Argon High`）
     * `网页平台预设` 下拉框（选项：`Google AI Studio (aistudio.google.com)` / `DeepSeek 网页版` / `Kimi 网页版` / `豆包网页版` / `通义千问` / `ChatGPT` / `自定义网页`，选中后自动填充对应网址与快捷键策略）
     * `网页地址 (Web URL)`（如 `https://aistudio.google.com/prompts/new_chat`）
     * `发送触发方式` 下拉框（`Ctrl + Enter (Google AI Studio 模式)` / `Enter 回车发送` / `点击发送按钮`）
     * **`[🌐 启动浏览器：登录账号并固定网页右侧模型设置]`** 醒目按钮（直接内嵌在弹窗中，点击即可拉起该节点的持久化浏览器窗口！）
     * 底部通用区：`能力勾选 (TEXT_LLM / VISION_VLM)`、`单包最大输入/输出 Token 与片段数`、`可用时段限制`、`轮询间隔 (20s) 与最长等待时间`。
   * **强制隐藏**：`Base URL`、`模型 (API Model Name)`、`API Key`、`本地模型文件`、`本地分词器`、`模型维度`、`排队提交 URL`、`排队状态 URL`。
2. **当选择 `🔌 标准/兼容 API (OpenAI / DeepSeek)` 时**：
   * **仅显示**：`显示名称`、`Base URL`、`模型名称`、`API Key` + `[⚡ 测试 API 连通性]` 按钮，以及底部通用区。
   * **强制隐藏**：`网页地址`、`登录 Profile`、`本地模型文件`、`本地分词器`、`排队提交/状态 URL`。
3. **当选择 `🏢 排队轮询 API` 时**：在标准 API 字段基础上额外显示 `排队提交 URL` 与 `排队状态 URL`。
4. **当选择 `💻 本地内置 ONNX` 时**：仅显示 `显示名称`、`本地模型文件`、`本地分词器`、`模型维度` 与 `能力勾选`。
5. **当选择 `📋 半自动剪贴板` 时**：仅显示 `显示名称` 与 `单包最大输入/输出 Token 与片段数`。

---

## Task 2：深度适配 Google AI Studio 与主流网页版 AI 的“常驻会话热接管”机制 (`app_core/models/web_ai_bridge.py`)

为了支持用户在 Google AI Studio (`https://aistudio.google.com/prompts/new_chat`) 等平台上手动选定任意最新模型（如 `Gemini 4 argon`）并开启特定思考档位（如 `High`），必须升级 `web_ai_bridge.py`：

1. **突破 Google 登录拦截与自动继承系统代理**：
   * 使用 `playwright.chromium.launch_persistent_context(user_data_dir=profile_dir, ...)` 时：
     * 优先尝试调用本机安装的真实浏览器内核：`channel="chrome"`（若未安装 Chrome 则回退 `"msedge"`，最后回退内置 Chromium）；
     * **必须传入反自动化检测参数**：`args=["--disable-blink-features=AutomationControlled", "--start-maximized"]` 及 `ignore_default_args=["--enable-automation"]`；
     * 自动读取 Windows 系统代理环境变量或系统注册表代理配置（同时允许在节点配置中选填代理地址如 `http://127.0.0.1:7890`），确保 Google AI Studio 与海外网页端顺畅连接。
2. **“常驻标签页热接管模式”（严禁粗暴刷新重置用户选好的模型参数！）**：
   * 当用户点击 **`[🌐 启动浏览器：登录账号并固定网页右侧模型设置]`** 后，程序保持该持久化浏览器实例存活（或复用同一个已打开的页面标签页）：
   * 用户在 Google AI Studio 页面右侧边栏选好目标模型（如 `Gemini 4 argon`）和参数（如 `Thinking: High`）后，在后续自动化批处理执行期间：
     * **严禁每次发送新包时调用 `page.goto()` 重新加载网页**（因为重载网页会导致右侧栏的模型选择与 `High` 档位被重置为默认值！）；
     * **页面内清理上一轮对话**：在 Google AI Studio 中，优先通过清除当前对话轮次（或点击页面内的 Clear/Delete turns，或当用户保存了带模型配置的专属 Prompt URL 时导航至该固定 URL），在 **100% 保留右侧边栏当前模型与参数设置** 的前提下，将新的合批任务包填入底部输入框（兼容 Google AI Studio 的 `textarea` / `ms-autosize-textarea` / `[contenteditable="true"]` 输入框及图片/文件上传控件）。
3. **精准触发发送与 20 秒轮询提取响应**：
   * 针对 Google AI Studio 预设（或选择了 `Ctrl+Enter` 模式的节点），填入文本后聚焦输入框并执行 `page.keyboard.press("Control+Enter")`（同时兼容点击页面上的 `Run` 按钮 `button:has-text("Run")`）。
   * 启动 **每 20 秒轮询检测器**：
     * 监测页面是否处于生成状态（如 `Stop` 按钮可见或流式文本长度仍在增长）；若正在生成则持续每 20 秒等待直至生成停止；
     * 生成完毕后，自动从最新的模型回复区域中提取 ```` ```json ... ``` ```` 代码块文本（或最后一段完整 JSON 数组），解析并拆包入库！

---

## Task 3：验证与重新打包 `AtomicCalculationDB.exe`

1. 运行 `pytest` 验证：
   * 节点编辑弹窗在切换 5 种不同 `node_type` 时，无关输入框能够 100% 正确隐藏、相关输入框正确显示；
   * Google AI Studio 预设模板能够自动配置正确的 URL、`Ctrl+Enter` 触发策略、反自动化检测启动参数及不重置右侧模型设置的会话复用逻辑。
2. 重新编译生成 **`AtomicCalculationDB.exe`** 并更新 `docs/` 截图。

---

## 实施记录（V2.3.5）

- 节点弹窗改为按 `node_type` 动态显示字段与区块；网页、API、排队 API、
  本地 ONNX、半自动剪贴板各自只显示相关配置。
- 新增网页预设、发送触发方式、代理地址、弹窗内启动浏览器与 API 连通性测试。
- `EndpointNode` 增加 `web_preset`、`send_mode`、`proxy_url` 持久化字段。
- `WebAIBridge` 改为后台常驻 Playwright 会话：Chrome → Edge → Chromium 回退、
  反自动化参数、系统/自定义代理、复用标签页且自动发送不调用 `page.goto()`。
- 网页回复支持 `Ctrl+Enter` / `Enter` / 点击发送，20 秒轮询、代码块/末尾 JSON
  提取；`WebAIPlaywrightClient` 已接入统一模型网关。
- 测试：**110 passed**。
- 截图：`docs/settings_web_ai_node_v235.png`。
- 发布包：
  - `release/AtomicCalculationDB_Patch_v2.3.5.zip`（390.8 KiB，SHA256 `bcc8dae50afd92b700762cb131d7347eead1edf69672849fdd1e010a15d93428`）
  - `release/AtomicCalculationDB_Full_v2.3.5.zip`（1,635.0 MB，9,944 个条目，SHA256 `837653a7b786fcb70dfdf4581ff6eded8ac5915fa8cca027c1790f6fc89c4141`）
  - 两个包均通过 `unzip -t`。
- 部署：构建时应用正在运行 V2.3.4，未覆盖安装目录；安装包已就绪。