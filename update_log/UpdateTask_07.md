# UpdateTask_07.md — `AtomicCalculationDB` (V2.6.0) Antigravity / Codex 双路径 Agent 平台对接

> **目标**：让用户自主选择运行方式，打通工作台 / 统一算力池与 Antigravity、Codex 等外部 Agent 平台的双向协作：
> 1. **路径一（MCP 双向工单）**：扩展 `KnowledgeMCPServer` 与 `RuntimeBridgeServer`，让 Antigravity / Codex 主动领包、触发解析、提交半自动提炼结果，并接管 v2.4.0 跨模型审计工单；同时提供设置开关控制是否允许 Agent 直接写入正式知识库。
> 2. **路径二（算力池 `AGENT_CLI` 节点）**：在 `EndpointPool` 与 `ModelGateway` 中新增本地 Agent CLI 节点（支持 `codex exec` 与自定义命令行预设），可与 API / 网页 AI 节点共同编排进接力链，用于工作台无人值守调度。

---

## 1. 公共底座加固

### 1.1 SQLite 单事务原子写库
- `app_core/db/sqlite_manager.py` 新增可重入的 `transaction()` 上下文管理器与 `_commit()`：在事务外正常提交，在 `with db.transaction():` 内所有 DML 合并为单次 SQLite 事务，异常时自动 `rollback()`。
- `app_core/models/semi_auto.py` 的 `finalize_run()` 将 `force` 模式删旧未确认卡、写入新卡与更新 `LLM_EXTRACTED` 状态全部包入 `db.transaction()`，并在事务提交后再同步清理 Qdrant，消除“写库中途失败留下半成品”的隐患。

### 1.2 Agent 来源可追溯（Provenance）
- `app_core/pipeline.py` 的 `_store_draft()` 支持可选 `provenance_override` 与 `provenance_source`。
- `semi_auto.import_reply()` / `finalize_run()` 记录实际提交的 Agent 标识（`MCP_AGENT` 或 `SEMI_AUTO_CLIPBOARD`）写入 `card_model_provenance`。
- `app_core/models/provenance.py` 的 `node_snapshot()` 新增 `AGENT_CLI`（`provider="agent_cli"`）与 `SEMI_AUTO_CLIPBOARD`（`provider="semi_auto"`）映射。

---

## 2. 路径一：MCP 双向工单与 v2.4.0 跨模型审计

### 2.1 权限配置与 GUI 开关
`app_core/config.py` 在 `mcp` 下新增：
- `mcp.allow_task_write`（默认 `true`）：是否允许 MCP Agent 发现任务、触发解析、生成任务包、暂存回复与导出审计包；
- `mcp.allow_direct_db_write`（默认 `false`）：是否允许 Agent 通过 MCP 直接把暂存的提取结果或审计结果写入正式知识库。

`app_core/ui/views/sync_mcp.py` 在「MCP Server」卡片中增加两个实时持久化开关：
- `启用 MCP 任务工单接口（允许 Agent 领包/解析/暂存回复与导出审计）`
- `允许 Agent 通过 MCP 直接写入正式知识库（提取入库与审计应用）`

### 2.2 `AgentWorkbenchService` 与环回桥接
新增 `app_core/sync_mcp/agent_workbench.py`，并接入 `RuntimeBridgeServer` / `BridgeWorkbench`（`app_core/runtime_bridge.py`）和 `MainWindow.external_data_changed` 信号：
- 当 GUI 运行时，外部 `--mcp` / `--mcp-sse` 进程通过 `runtime_bridge.json` 环回代理在 GUI 进程内执行读写，不触发 Qdrant 本地锁，且写入正式库后自动刷新 GUI 界面；
- 当 GUI 未运行时，独立 `--mcp` 进程直接读取本地服务，并在每次权限校验时重读 `workspace/config.yaml`，无需重启 MCP 即可生效开关变更。

### 2.3 新增 10 个 MCP 工单工具（共 16 个工具）
在 `app_core/sync_mcp/mcp_server.py` 中新增：
1. `list_workbench_tasks(status, limit)`：列出待解析/待提炼/`NEEDS_OCR` 文献、未完成半自动运行与近期审计导出；
2. `parse_document_for_agent(doc_id)`：触发本地 PyMuPDF/MinerU 解析并生成 `bundles.json`（不调模型）；
3. `create_semi_auto_batch_run(doc_ids, mode, max_batch_tokens)`：按预算拆包持久化到 `workspace/semi_auto/<run_id>/`（默认 `25,000` tokens）；
4. `list_semi_auto_runs(include_finalized)`：查看各批次状态与待入库状态；
5. `get_semi_auto_batch_prompt(run_id, batch_id)`：直接拉取批次提示词、文献 ID 与 item 列表；
6. `submit_semi_auto_batch_reply(run_id, batch_id, reply_json, agent_name, agent_model)`：按现有宽松归一化规则校验并写入 `reply.txt`（不改正式库）；
7. `finalize_semi_auto_run(run_id, agent_name, agent_model)`：将已就绪批次原子写入正式库（**受 `mcp.allow_direct_db_write` 保护**）；
8. `export_document_audit_ticket(doc_id, include_source, include_evidence, coverage_audit)`：导出 v2.4.0 审计包并返回 `report_id` / `report_hash`；
9. `get_audit_ticket_content(report_id, part, include_prompt)`：读取指定分片的 `audit_prompt*.md` 与 `report*.md`；
10. `submit_audit_ticket_result(report_id, audit_json, auditor_name, auditor_model, apply_to_db, apply_verified)`：校验并暂存 `agent_audit_*.json` 到导出目录；当 `apply_to_db=true` 且开启 `mcp.allow_direct_db_write` 时直接应用审计结果到正式库（仍受 `apply_verified=false` 保护），未开启直写开关时保留暂存文件并明确提示原因。

---

## 3. 路径二：算力池 `AGENT_CLI` 节点

### 3.1 `EndpointNode` 扩展
`app_core/models/endpoint_pool.py`：
- `NODE_TYPES` 新增 `"AGENT_CLI"`（推荐默认容量 `20,000` 输入 / `6,000` 输出 / `8` 片段）；
- 新增字段：`cli_preset`（`codex` / `custom`）、`cli_command`、`cli_args`、`cli_workdir`、`cli_timeout_sec`、`cli_sandbox`（默认 `read-only`）。

### 3.2 `AgentCLIClient` 与沙箱隔离执行
新增 `app_core/models/agent_cli.py`，并在 `app_core/models/gateway.py` 接入 `AgentCLIClient`：
- **`codex` 预设**：默认构造 `codex exec --skip-git-repo-check --sandbox <sandbox> --output-last-message <reply_file> [-m <model>] [extra_args...] -`，通过 stdin 传入 prompt 并从输出文件读取最终回复；
- **`custom` 预设**：支持 `{prompt_file}`、`{output_file}`、`{model}`、`{workdir}` 占位符，未指定文件占位符时走 stdin/stdout；
- 默认在 `workspace/agent_cli_tmp/` 下创建一次性隔离目录执行，避免 CLI Agent 误改动项目目录文件；
- 支持代理注入、超时控制与 `RelayExecutor` 失败退回接力。

### 3.3 算力池编辑表单与测试
- `app_core/ui/model_pool_dialogs.py` 新增 `AGENT_CLI` 动态表单字段与 `🧪 测试 Agent CLI` 按钮；
- `app_core/ui/model_pool_panel.py` 列表展示与工具提示适配 `AGENT_CLI` 节点及连通性检测。

---

## 4. 测试与发布

- 新增 `tests/test_v260.py`：覆盖 SQLite 事务回滚、`semi_auto.finalize_run()` 原子性、`AGENT_CLI` 节点往返/命令构造/`ModelGateway` & `RelayExecutor` 接力、`RuntimeBridgeServer` + `BridgeWorkbench` 全流程（半自动提取、v2.4.0 审计导出/提交/应用、权限开关拦截与动态切换）、`KnowledgeMCPServer` 16 工具注册与调用、以及 `EndpointNodeDialog` / `SyncMCPView` UI 控件测试。
- 全量回归：`python -m pytest -q` **169 passed**（Python 3.11.9）。
- 发布产物（均通过 `zipfile.ZipFile.testzip()` 完整性校验）：
  - `release/AtomicCalculationDB_Patch_v2.6.0.zip`（480.6 KiB，492,090 字节，98 个文件，SHA256 `a39f177422a65898bf933cca3ee3eb7ba52eebd63b66fa424873a2a4c72057a1`）
  - `release/AtomicCalculationDB_Full_v2.6.0.zip`（1,635.1 MB，1,714,524,719 字节，9,963 个文件，SHA256 `c8247c862701f1849a4c7dc5bca6e64f2fd282b8eefb914b1fecfb01aecb7b0f`）
- `dist/` 已同步至 `2.6.0`，`codebase-memory-mcp` 已重新索引（2,835 节点 / 14,423 边）。
