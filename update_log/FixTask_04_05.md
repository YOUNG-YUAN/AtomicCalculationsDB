# FixTask_04_05.md — `AtomicCalculationDB` (V2.3.6) 网页 AI 登录 Worker 已删除崩溃修复任务书

> 说明：工作目录原先没有 `FixTask_04_05.md`；V2.3.5 的功能实现按
> `FixTask_04_04.md` 完成。本文件记录随后发现的 V2.3.6 登录崩溃热修复。

---

## 1. 用户现象

用户启动 Google AI Studio 网页节点，完成 Google 登录并在右侧固定
`Gemini 4 Argon` + `Thinking: High` 后，应用弹出：

```text
程序遇到错误，详情已写入 workspace/logs/app.log。
libshiboken: Internal C++ object (_LoginWorker) already deleted.
```

浏览器窗口本身正常，登录也已完成，但应用没有给出稳定的“浏览器已启动”
提示，再次点击登录按钮时直接崩溃。

---

## 2. 根因

`app_core/ui/model_pool_panel.py` 的登录按钮使用 `_LoginWorker(QThread)`：

```python
worker.finished.connect(worker.deleteLater)
self._login_worker = worker
```

`deleteLater()` 在第 1 次启动完成后销毁了底层 C++ `_LoginWorker` 对象，
但面板仍保留 `self._login_worker` 的 Python 包装引用。用户再次点击
“🔑 登录网页端”时执行：

```python
if self._login_worker is not None and self._login_worker.isRunning():
```

访问已经销毁的 C++ 对象，触发 libshiboken 崩溃。

---

## 3. 修复

### 3.1 先清理引用，再安全删除

新增 `_login_finished(worker)`：

```python
def _login_finished(self, worker: _LoginWorker) -> None:
    if self._login_worker is worker:
        self._login_worker = None
    worker.deleteLater()
```

`finished` 信号现在连接到该槽，而不是直接连接 `worker.deleteLater`。

### 3.2 防御旧引用

`_login_node()` 读取 `isRunning()` 时捕获 `RuntimeError`；即使旧版本遗留
了失效包装对象，也会丢弃引用并允许重新启动，而不是让整个应用崩溃。

### 3.3 明确成功提示

浏览器启动成功后弹出：

- 浏览器已启动并保持常驻；
- 请在该窗口完成登录并固定模型/思考档位；
- 登录完成后**无需再次点击“启动浏览器”**，也没有额外的“通过”按钮；
- 后续自动发送复用同一标签页和当前模型设置。

失败时弹出明确的错误提示，不再只写日志。

### 3.4 保持 V2.3.5 的常驻会话行为

登录窗口由 `WebAIBridge` 的常驻 Playwright 会话线程持有，关闭面板不会
关闭浏览器；`WebAIPlaywrightClient` 后续发送不调用 `page.goto()`。

---

## 4. 验证

新增回归测试：

```text
test_login_worker_reference_is_cleared_before_delete
```

验证 worker 完成后先清空面板引用，再调用 `deleteLater()`。

完整测试：

```text
111 passed
```

---

## 5. 构建与部署

- 版本：**V2.3.6**（build 20261102，channel v2.3）
- 发布包：
  - `release/AtomicCalculationDB_Patch_v2.3.6.zip`（391.2 KiB，SHA256 `b7d37731767cbdf98902b52363a16d0bcc1ac933072a484cf4c2188b828d626a`）
  - `release/AtomicCalculationDB_Full_v2.3.6.zip`（1,635.0 MB，9,944 个条目，SHA256 `6fb42d3503e3abbeea5b2b7eeb49d63d1ef7aca8720b3f05b6d11bd4c406e88d`）
  - 两个包均通过 `unzip -t`。
- 构建时安装目录的应用仍在运行，因此未覆盖安装目录；更新包已就绪。
- 安装方式：设置页导入补丁包，或关闭应用后部署。
