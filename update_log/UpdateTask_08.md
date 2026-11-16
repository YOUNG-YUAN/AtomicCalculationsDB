# UpdateTask_08.md — `AtomicCalculationDB` (V2.7.0) 文献处理五步流式进度条与多端深度归纳体系

> **目标**：全面重构文献处理的工作流可视化与交互体系：
> 1. **五步流式进度条（Stepper Widget）**：
>    - `① 提取文字` ── `② 提取关键信息` ── `③ 交付AI速览` ── `④ 写入数据库` ── `⑤ 交付AI深度归纳`
>    - 节点状态：已完成（绿底/金底白字）、进行中（高亮脉冲）、异常中断（红框告警）、未开始（灰圈）。
>    - 连接线：已完成为实心绿线，过渡为半绿半灰渐变线，未完成为灰线。
>    - **双阶光晕反馈**：
>      - 第 ④ 步「写入数据库」完成时，整体进度条**冒绿光**（表明基础数据已入库可用）；
>      - 第 ⑤ 步「交付AI深度归纳」完成时，整体进度条**冒金光**（表明顶层 L1 Master 方法论主卡已形成，彻底吃透）。
> 2. **级联回滚与光芒熄灭机制**：
>    - 上游步骤修改（如从 ① 重新提取文字或从 ② 重新分析信息），下游依赖的过期数据自动失效，未确认卡片回滚，绿光/金光熄灭。
> 3. **UI 按钮去黑话化**：
>    - 废除“强制重提”、“智能增补”、“强制重新提炼”等含糊其辞的按钮。
>    - 统一为以“从这一步开始”为核心的分步触发按钮：
>      - `[① 从文字提取开始]`、`[② 从信息提取开始]`、`[③ 从AI速览开始]`、`[④ 提交写入数据库]`、`[✨ ⑤ 启动深度归纳]`。
> 4. **第 ⑤ 步三端深度归纳通道**：
>    - 网页版 AI：一键导出所有 L2 细节卡片的高密度 Markdown 归纳 Prompt，支持粘贴回填 Master JSON；
>    - Codex Agent CLI：通过 V2.6.0 `AGENT_CLI` 沙盒引擎静默调用 `codex exec` 执行深度综合并自动入库；
>    - Antigravity Agent MCP：扩展工单工具，支持在 MCP 对话或工单中直接调用 `submit_deep_synthesis_result`。
> 5. **全场景联动呈现**：
>    - 深度工作台 / 文献详情弹窗内展示完整大尺寸交互式进度条；
>    - 文献库主界面列表项新增「流程进度」列展示迷你 5 节点指示器与右键分步启动菜单。

---

## 1. 状态机与级联回滚引擎 (`pipeline_stepper.py`)

- 定义五步标准枚举：
  - `STEP_1_TXT`: 提取文字（PDF/CAJ 解析、OCR 文本层、Markdown 生成）；
  - `STEP_2_INFO`: 提取关键信息（4通道雷达扫描、图表公式定位、`ContextBundle` 生成）；
  - `STEP_3_FAST`: 交付AI速览（L2 候选卡片提炼，支持 API 或 网页版剪贴板）；
  - `STEP_4_DB`: 写入数据库（L2 卡片校验提交写入 SQLite/Qdrant，触发**绿光**）；
  - `STEP_5_DEEP`: 交付AI深度归纳（L1 Master 主卡片生成与争议裁决，触发**金光**）。
- 状态计算器 `compute_document_pipeline_status(doc, cards)`：
  - 自动根据现有文献状态与关联卡片判定各步骤状态与光晕类型（无需数据库破坏性迁移，老数据即刻呈现绿光/金光）。
- 级联回滚函数 `rollback_pipeline_from_step(db, workspace, doc_id, target_step)`：
  - 严格清理下游依赖产物与未核准卡片，安全重置状态。

---

## 2. 三端深度归纳服务 (`deep_synthesis.py`)

- `export_master_synthesis_prompt(db, doc_id)`：
  - 提取该文献在库的所有 L2 卡片（`STRAT`, `MODEL`, `CODE`, `PITFALL`, `CONC`），结合 `MASTER_SYSTEM_PROMPT` 组装结构化 Prompt。
- `apply_master_synthesis_payload(db, qdrant, doc_id, payload)`：
  - 严格校验 `MasterCardDraft`，生成 `KNW-L1-MASTER-*` 卡片并建立 `supporting_doc_ids=[doc_id]` 关联，写入 SQLite 并同步向量引擎，点亮**金光**。
- `synthesize_with_agent_cli(db, qdrant, workspace, doc_id, endpoint_node)`：
  - 通过 `AgentCLIRunner` 在临时工作区运行 `codex exec` 完成自动化深度归纳。

---

## 3. UI 组件与视图改造

- `app_core/ui/step_progress_widget.py`：
  - 实现标准 `StepProgressWidget` 与紧凑型 `MiniStepProgressWidget`；
  - 绘制五色环、流动渐变连线、高亮脉冲与 QPainter / DropShadow 光晕（绿光与金光）。
- `app_core/ui/views/library.py`：
  - 替换顶部重提按钮组为标准化分步操作栏；
  - 表格增加「流程进度」列，嵌入迷你 5 节点微光指示器；
  - 右键菜单支持「▶ 从此步开始」二级菜单。
- `app_core/ui/dialogs.py` (`DocumentDetailDialog`) 与 `semi_auto_dialog.py`：
  - 顶部集成 `StepProgressWidget`；
  - 提供分步操作按钮与深度归纳向导弹窗。

---

## 4. MCP 双向扩展

- 新增工具：
  - `export_deep_synthesis_task(doc_id)`
  - `submit_deep_synthesis_result(doc_id, master_json, agent_name, agent_model)`
