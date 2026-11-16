# FixTask_04_06.md — `AtomicCalculationDB` (V2.3.7) Google AI Studio 真实浏览器 CDP 全自动接管

> 参考 `C:\Users\Young\Documents\Python\AIPowerpointGenerator\UpdateTask_13.md`
> 及其 `apg/annotator/browser_autopilot.py` 的真实 Chrome/Edge CDP 方案，
> 对 V2.3.5/V2.3.6 的 Playwright 网页节点实现进行升级。

---

## 1. 旧实现的问题

- 使用 Playwright 自带 Chromium 的 `launch_persistent_context`，Google 登录容易
  被判定为自动化环境，并出现 `--no-sandbox` 警告；
- 没有自动选择 Google AI Studio 右侧的模型与 Thinking 深度，用户必须手动操作；
- 登录按钮完成后没有等待 Google 登录/编辑器/模型面板真正就绪；
- 回复轮询依赖“文本稳定”，没有严格使用 Stop 按钮消失作为生成完成门控；
- 可能把用户提问回显误当成模型回复；
- 登录 worker 在第二次点击时存在已删除 QThread 崩溃（V2.3.6 已修复）。

---

## 2. 新实现

### 2.1 本机真实浏览器 CDP

新增 `app_core/models/aistudio_autopilot.py`：

- 查找本机 Chrome → Edge；
- 使用独立 Profile 目录；
- 通过 `--remote-debugging-port` + `--user-data-dir` 启动真实浏览器；
- 通过 `playwright.chromium.connect_over_cdp` 接管；
- 断开 Playwright 时不关闭用户浏览器和登录会话；
- 支持自定义 `web_cdp_port`（默认 9333）与代理参数。

### 2.2 登录、模型与 Thinking 自动配置

- `open_login()` 现在等待 Google 登录完成，并等待 Angular 编辑器/模型入口可见；
- 自动打开 Run settings 模型面板，选择节点配置的 `网页模型`
  （默认 `Gemini 4 Argon`）；
- 自动把 Thinking 选为 `High`（可改为 Medium/Low）；
- 成功后 QThread 才返回，界面弹出“浏览器已启动/登录与模型已配置”提示。

### 2.3 严格生成完成判定

- 使用多组 `Stop` 选择器；Stop 可见时绝不接受回复；
- 只从模型专属节点（`model-response` / `data-turn-role=model`）及其中代码块
  提取候选文本；
- 过滤提问词回显；
- 只接受完整可解析 JSON；
- 记录发送前的回复快照，避免把上一轮旧回复当成本轮结果；
- 网页回复默认每 1.5 秒轮询一次，可在节点中调整。

### 2.4 UI

网页节点新增：

- `网页模型（自动选择）`；
- `Thinking 深度`；
- `CDP 调试端口`；
- `网页回复轮询`。

预设 Google AI Studio 自动填入模型、Thinking、URL、Ctrl+Enter 和 9333 端口。

---

## 3. 验证

- 新增 `tests/test_v235.py` 中的 AI Studio 驱动测试：
  - Stop 可见时不返回；
  - Stop 消失后从模型节点提取 JSON；
  - 提问词回显过滤。
- 真实本机 Chrome CDP 启动/连接/读取页面烟测通过。
- 全量回归：**112 passed**。

---

## 4. 构建与部署

- 版本：**V2.3.7**（build 20261102，channel v2.3）
- 发布包：
  - `release/AtomicCalculationDB_Patch_v2.3.7.zip`（399.0 KiB，SHA256 `c3fc49545ae1ecfaa65282079950dc9230901687ba215f1d5584d6a5c837cdbe`）
  - `release/AtomicCalculationDB_Full_v2.3.7.zip`（1,635.0 MB，9,944 个条目，SHA256 `d49a2829024028f456123e2dafb4648c038d6471bf7604a85f07e92ef0bae5f0`）
  - 两个包均通过 `unzip -t`。
- 构建时应用仍在运行（V2.3.6），未覆盖安装目录。
- 安装后如旧版网页浏览器仍占用 Profile，请关闭旧浏览器窗口再启动。

---

## 5. V2.3.8 模型显示修复

用户反馈：节点类型已经选为“网页版 AI”，列表仍显示 `🧠 deepseek-flash`。
原因是旧配置中网页节点的 `model` 字段残留了 API 模型名，节点列表使用了
`node.model or node.web_model`。

修复：

- `EndpointNode.__post_init__` 对 `WEB_AI_PLAYWRIGHT` 清空 API `model` 字段；
- 节点列表与工具提示改用 `web_model_settings()`，优先显示网页模型；
- `WebAIPlaywrightClient.model` 使用网页模型（或 Google 预设模型），不再
  使用旧的 API 模型名；
- 编辑网页节点保存时不再写回隐藏的 API 模型字段。

验证：使用用户实际 `config.yaml` 只读加载后，节点显示为
`Gemini 4 Argon` / `Gemini 3.8 Flash`，不再出现 `deepseek-flash`。

V2.3.8 发布包：

- `release/AtomicCalculationDB_Patch_v2.3.8.zip`（399.1 KiB，SHA256
  `c793b2f16db02ec710d60301a553c4aad8c35842753b978e70c9fd0df6c8bd12`）
- `release/AtomicCalculationDB_Full_v2.3.8.zip`（1,635.0 MB，9,944 个条目，
  SHA256 `453490d4e2d1e51bae89768938db63349df7c85fa200cf9ed084231c97003583`）
- 两个包均通过 `unzip -t`；全量回归 **112 passed**。

---

## 6. V2.3.9 自动 Run 触发修复

用户实测：模型与 Thinking 已自动设置，提示词也已填入，但没有自动发送。

真实 AI Studio DOM 检查发现：

- 底部真实按钮文本是 `Run Ctrl ↵`，不是纯 `Run`；
- 对话中同时存在多个 `Rerun this turn` 按钮；
- 旧的宽泛选择器 `button[aria-label*='Run' i]` 会先匹配旧的
  `Rerun this turn`，导致重跑旧回复，而新提示词仍留在输入框；
- 生成中 Run 按钮会变为禁用状态。

修复：

- 新增 `find_run_button()`，遍历可见按钮，要求标签以 `run` 开头；
- 排除 `rerun`、`Run settings`、`running` 以及 disabled 按钮；
- 精确点击底部当前 Run；找不到时聚焦输入框并回退 `Control+Enter`；
- 新增 `test_run_button_finder_skips_rerun_and_run_settings`。

V2.3.9 全量回归 **113 passed**；发布包：

- `release/AtomicCalculationDB_Patch_v2.3.9.zip`（399.5 KiB，SHA256
  `18e2256e3fbb38507f14f234deb41452272ec6812b9025e2fb4a60e3e66b9280`）
- `release/AtomicCalculationDB_Full_v2.3.9.zip`（1,635.0 MB，SHA256
  `f34ba08dd38afee05b3fb60ad84ff10f8bde60010ca54c052c2af6181691aebe`）
- 两个包均通过 `unzip -t`。安装目录仍为 V2.3.8，构建时应用在运行，
  未覆盖安装目录。
