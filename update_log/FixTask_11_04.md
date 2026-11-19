# FixTask_11_04.md — `AtomicCalculationDB` (v3.0.4) 网页大模型半自动未完成任务包分层智能检索恢复与手动切换支持

> **版本定位**：从 `v3.0.3` 平滑升级至 `v3.0.4`（Build `20261118`）。  
> **任务类型**：Z 版本日常补丁维护（遵循：不改动 `README.md`、不发起 GitHub Release，仅生成本地 `Patch` 与 `Lite` 构建包供本地更新）。

---

## 1. 现象复现与问题诊断

### 1.1 用户反馈
用户在更新重启软件后，打开半自动提问台时发现：
> “我更新了，但是我看不到之前的提问包了”

### 1.2 根因排查
1. **多文献任务包与单文献检索不匹配**：
   - 用户之前生成的未完成任务包 `semi_20261118_182821_259205` 包含了 3 篇文献：
     - `DOC-PAP-0000-8C9C21`
     - `DOC-PAP-0000-772B5A`
     - `DOC-PAP-0000-40F557`
     共 9 份提问包（其中 Batch 02~08 已全部导入通过，待导入 Batch 01 与 09）。
   - 更新重启软件后，界面上的文献选中状态被重置（例如默认只选中第 1 行单篇文献，或用户从 `8C9C21` 单篇文献详情页打开工作台）。
   - 旧版 `find_unfinished_run` 采用了严格的**集合完全相等匹配**（`actual == wanted`）。
   - 当 `wanted` 仅有 1 篇（如 `{'DOC-PAP-0000-8C9C21'}`）而任务包中有 3 篇时，`actual == wanted` 为 `False`，导致系统直接返回 `None`。
   - `SemiAutoBatchDialog` 误以为是全新的无提问包状态，展示为清空的初始界面。
2. **缺乏手动查看与切换未完成任务包的 UI 入口**：
   - 用户无法在界面上一键列出或恢复工作区已有的未完成任务包。

---

## 2. 解决方案与核心改造

### 2.1 任务包层级智能检索恢复（`models.semi_auto.find_unfinished_run`）
重构 `find_unfinished_run` 为分层智能命中机制：
1. **优先（精准命中）**：文献集合完全相等（`actual == wanted`）；
2. **次优（子集匹配）**：查询文献为未完成任务包的子集（`wanted.issubset(actual)`），如从单篇文献详情页或列表部分选中打开时，自动命中整组任务包；
3. **兜底（交集匹配）**：查询文献与未完成任务包有交集（`wanted & actual`）；
4. **默认（无指定时）**：直接返回最近活跃的未完成任务包。

### 2.2 工作台文献清单双向同步（`SemiAutoBatchDialog.__init__`）
当成功恢复任务包时，自动将工作台的 `self.doc_ids` 同步扩展为该任务包包含的全部文献清单，使文献列表展示与提问包行列表完美对应。

### 2.3 新增未完成任务包快速切换入口（`SemiAutoBatchDialog._switch_run`）
在半自动工作台控制栏增加 `[🔄 恢复/切换未完成任务包]` 按钮：
- 一键列出当前工作区中所有处于未完成状态（`WAITING`）的任务包（标注文献名称、提问包总数与已导入进度）；
- 点击任一任务包即可秒级切换并恢复，即使忘记之前选了哪些文献也能轻松找回。

---

## 3. 关联知识卡 AI 模型溯源防混淆加固（排除物理原子模型 model 字段干扰）

### 3.1 诊断与根因
在文献详情页右侧知识卡列表中，用户发现部分知识卡右侧显示了非 AI 模型名称（如 `Born-Oppenheimer`、`two-manifold MCDF-AL`、`point nucleus` 等）。
经深度排查：
- 知识库中所有 3,655 张卡片的真实来源表 `card_model_provenance` 记录均为真实的 `deepseek-v4.1-flash-1017`，无一例外；
- 原 `ui/dialogs.py` 的 `get_card_model_name()` 函数在检索卡片来源时，优先遍历了 `card["structured"]` 中的 `model` 键名；
- 物理领域知识卡（尤其是 `MODEL` 型知识卡）在 `structured` 中存储了**物理原子理论模型**（如 `model="Born-Oppenheimer"`），导致解析器误将物理模型当成提取该卡片的 AI 大模型输出，造成显示混淆。

### 3.2 优化修复
1. 将 `card_model_provenance` 表的权威溯源记录提升为第一优先级；
2. 在 `structured` 和卡片顶层字段中，严格只检索 `extractor_model`、`agent_model`、`model_name`，剔除泛化的物理理论模型键名 `model`；
3. 经重新扫描，`deepseek` 分类下全部 27 篇文献的 3,655 张知识卡统一正确展示为 `deepseek-v4.1-flash-1017`，非 DeepSeek 污染数为 0。

---

## 4. 右键菜单“从指定步骤开始”AttributeError 修复

### 4.1 诊断与根因
用户在文库列表中右键点击文献并选择“▶ 从第 ① 步开始：重新提取文本”或“▶ 从第 ② 步开始：重新提取关键信息”时，触发弹窗报错：
```
'LibraryView' object has no attribute 'step3_semi_button'
```
排查原因：
- 在重处理状态切换函数 `LibraryView._set_reprocess_busy` 中，历史遗留代码尝试禁用 `self.step3_semi_button`；
- 该按钮在早期的“统一第 ③ 步入口”改造中已被整合为单一的 `step3_api_button`，变量未在 `__init__` 中定义，导致右键触发重处理流程时发生 `AttributeError` 阻断。
- 另外，右键菜单中调用 `_run_reprocess("force")` 时未显式传入当前右键的 `target_doc_ids=[doc_id]`，且未跳过二次确认弹窗（前一步弹窗已确认）。

### 4.2 优化修复
1. 重构 `LibraryView._set_reprocess_busy`，使用安全安全的 `getattr` 动态遍历按钮列表，并将不存在的 `step3_semi_button` 替换为现存的 `step4_audit_button`；
2. 在右键菜单第 ①/② 步处理逻辑中，显式传入当前文献 `target_doc_ids=[doc_id], skip_confirmation=True`，确保仅重跑目标文献且不重复弹窗。

---

## 5. 工作台窗口控件规格化（增加最小化、全屏/最大化与关闭按钮，解决主页面移动受阻问题）

### 5.1 现象与需求
- 用户反馈：半自动提炼工作台（`SemiAutoBatchDialog`）尺寸较大（1200x880），默认作为 `QDialog` 弹窗在 Windows 下仅有右上角的 `[×]` 关闭按钮，缺乏最小化与全屏控件；
- 用户在提炼过程中需要频繁与网页端大模型配合（复制提问包、前往浏览器粘贴问答），有时需要移动或查阅被遮挡的主页面；
- 但由于提炼窗口浮在主页面上层且无最小化按钮，用户无法将窗口缩至任务栏以腾出空间操作移动主窗口。

### 5.2 解决方案
1. **注入标准原生顶层窗口标志**：
   - 在 `SemiAutoBatchDialog`、`DeepSynthesisDialog`（第 ⑤ 步深度归纳工作台）及 `DocumentAuditDialog`（第 ④ 步校对工作台）的 `__init__` 中显式设置：
     ```python
     self.setWindowFlags(
         Qt.Window
         | Qt.WindowMinMaxButtonsHint
         | Qt.WindowCloseButtonHint
     )
     ```
   - 在 Windows 系统下原生渲染完整的 `[-]` 最小化、`[□]` 最大化/全屏切换及 `[×]` 关闭三大控制按钮。
2. **最小化时主窗体动态解锁**：
   - 重写 `changeEvent(event)`，在监听到 `QEvent.WindowStateChange` 且窗口被最小化时，通过 Windows API 动态解除对顶层父窗口（主界面）的禁用锁定状态；
   - 用户将工作台最小化后，可自由点击、拖拽移动或查阅底层主界面；当用户从任务栏还原工作台时，自动恢复模态上下文。

---

## 6. 验证与交付

1. **测试套件**：
   - 新增 `tests/test_v304.py`，完整覆盖精准匹配、单篇子集匹配、部分选中匹配、交集匹配、无参数匹配、物理理论模型名称过滤、`_set_reprocess_busy` 源码安全断言，以及三大工作台窗口控件标志与最小化事件测试；
   - 运行全量测试套件，**235 个测试全部通过（100% Passed）**。
2. **打包与部署**：
   - 同步 `app_core` 至 `C:\Normal Program Files\AtomicCalculationDB\` 及 `dist\`；
   - 更新本地构建包 `release/AtomicCalculationDB_Patch_v3.0.4.zip` 与 `release/AtomicCalculationDB_Lite_v3.0.4.zip`。

