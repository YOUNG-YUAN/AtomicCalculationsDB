# FixTask_10_04.md — 顶部核心操作按钮范围选择弹窗接入与目录树选刊支持 (v2.9.4)

## 1. 现象与需求背景

用户提出反馈：
> “非常好，这几项（图1）点之后也弹出（图3）这样的框，点选择文献弹出图2一样的框”
- 附图 1（`media_1794668766838_dd23ac5a.png`）：
  主界面顶部三个核心处理按钮：
  1. `⚡ ③ 选中文献 AI 速览`（后台 API 全自动提炼）
  2. `📋 ③ 网页版半自动速览`（网页版超长思维链半自动批处理）
  3. `✨ ⑤ 启动深度归纳 (冒金光)`（宏观 Master 卡片与流水线金光达成）
- 附图 3（`media_1794668895561_723157f3.png`）：
  范围选择弹窗（包含选择所有文献、清空所有选择、选择文献(文库列表)...、仅处理选中文献、处理当前筛选结果、处理全库所有文献三项单选、实时状态/预估估算与确定/取消）。
- 附图 2（`media_1794668824972_69484d52.jpg`）：
  带左侧“我的文库”层级目录树与右侧多选清单列表的 `DocSelectDialog`。

此前，这三个按钮在未选中列表文献时会直接弹出 toast 阻断（“请先在文献列表中选中...”），且用户无法在触发任务前直观预览与动态按需调整处理范围（如切换到全库、切换到当前筛选目录、或临时打开层级目录多选框挑选文献）。

---

## 2. 方案设计与具体实现

### 2.1 统一任务范围选择对话框 (`TaskScopeDialog`)
在 `app_core/ui/task_scope_dialog.py` 中实现了通用的任务范围选择器：
1. **任务场景自适应文案与样式**：
   - `step3_api`：展示 API 速览介绍、预估消耗 Token 数量（4 路并行），确定按钮为蓝色 `▶ 开始执行 AI 速览`；
   - `step3_semi`：展示零 Token 成本网页端打包说明，确定按钮为深蓝 `📋 打开半自动工作台`；
   - `step5_deep`：展示第 ⑤ 步宏观全景与点亮金光说明，确定按钮为金橙色 `✨ 打开深度归纳工作台`；
2. **快捷范围选择与状态联动**：
   - `✓ 选择所有文献`：一键加载全库所有文献并激活单选；
   - `✗ 清空所有选择`：重置当前选区；
   - `🔍 选择文献 (文库列表)...`：调出带左侧“我的文库”层级目录树的 `DocSelectDialog`，支持点选、框选、连选与跨分类自由选择；
3. **单选范围精细支持**：
   - `仅处理选中文献 (已选 X 篇)`；
   - `处理当前筛选结果 (共 Y 篇)`（支持按状态或当前选定分类目录过滤）；
   - `处理全库所有文献 (全库共 Z 篇)`；
   - 切换单选时动态响应估算文案更新。

### 2.2 深度归纳工作台支持多文献按篇切换 (`DeepSynthesisDialog`)
此前 `DeepSynthesisDialog` 仅支持传入单篇 `doc_id: str`。为了支持用户在 `TaskScopeDialog` 中一次性挑选多篇目标文献执行第 ⑤ 步：
1. **参数兼容升级**：
   - 支持 `doc_id: str | list[str]`，内部规范化为 `self.doc_ids` 列表；
2. **文献切换下拉控件 (`QComboBox`)**：
   - 当传入多篇文献时，在顶部标题栏自动呈现文献下拉选择框（展示 DOC-ID 与文献标题摘要），用户可随时下拉切换当前精读文献；
   - 切换时自动重新加载当前文献的进度指示器、L2 卡片统计、等电子序列与 Prompt；
3. **连续完成问询与顺畅流转**：
   - 当一篇文献完成深度归纳入库（网页版提交或 Codex 沙盒成功）后，若存在待处理的下一篇文献，提示用户“是否继续对下一篇文献执行深度归纳？”，点击是即可平滑切入下一篇，零打断完成批量宏观精读。

### 2.3 主界面槽函数与任务引擎管道全面对接 (`LibraryView`)
在 `app_core/ui/views/library.py` 中：
1. **新增 `_current_filtered_doc_ids()` 辅助方法**：
   - 自动获取当前选定分类目录或状态筛选条件下的全部文献 ID；
2. **改造三大核心按钮槽函数**：
   - `_run_selected_step3_api`：弹出 `TaskScopeDialog`，用户确认后将目标文献 ID 传入 `_run_reprocess("incremental", target_doc_ids=...)`；
   - `_open_semi_auto_force`：弹出 `TaskScopeDialog`，确认后以选定文献集合启动 `SemiAutoBatchDialog`；
   - `_open_selected_step5_deep`：弹出 `TaskScopeDialog`，确认后以选定文献集合启动增强版 `DeepSynthesisDialog`；
3. **`_run_reprocess` 适配**：
   - 增加 `target_doc_ids` 与 `skip_confirmation` 参数，避免在弹窗确认后重复弹出冗余询问框。

---

## 3. 验证与回归测试

1. **单元与集成测试**：
   - 在 `tests/test_v290.py` 中新增 `test_task_scope_dialog_and_deep_synthesis_multidoc`：
     - 测试 `TaskScopeDialog` 在 `step3_api`、`step3_semi`、`step5_deep` 各场景下的初始化、单选切换、快速全选/清空及 `get_target_doc_ids()` 返回结果；
     - 测试 `DeepSynthesisDialog` 支持多篇文献列表、下拉切换文献、标题动态更新与数据重载；
2. **全量自动化测试**：
   - 全库 182 个自动化测试用例全量通过（**182 passed in 24.45s**）；
3. **打包与校验**：
   - 版本号提升至 **`2.9.4`**（`app_core/version.json`）；
   - 同步至 `dist/app_core/`；
   - 构建增量更新包 `release/AtomicCalculationDB_Patch_v2.9.4.zip`；
   - 构建完整发布包 `release/AtomicCalculationDB_Full_v2.9.4.zip`；
   - 经 Python `zipfile.testzip()` 完整性校验无误。
