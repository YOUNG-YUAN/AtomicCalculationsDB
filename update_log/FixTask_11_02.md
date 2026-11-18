# FixTask_11_02.md — `AtomicCalculationDB` (v3.0.2) 文库选择器表头点击多维正反双向排序能力增强与体验统一

> **版本定位**：从 `v3.0.1` 平滑升级至 `v3.0.2`（Build `20261118`）。  
> **任务类型**：Z 版本日常补丁维护（遵循：不改动 `README.md`、不发起 GitHub Release，仅生成本地 `Patch` 与 `Lite` 构建包供本地更新）。

---

## 1. 任务背景与核心诉求

### 1.1 用户反馈
> “全库本地修复与清洗，AI快速理解，AI校对核查，AI深度归纳里面的‘选择文献（文库列表）’里面表头这一栏，需要允许排序，正序逆序这种。这一点我们之前已经在文献清单的列表里实现了，你copy逻辑过去就可以。v3.0.2，补一个FixTask吧”

### 1.2 涉及组件与业务场景
在系统日常运维与批量 AI 调度中，以下四大业务模块均提供了“🔍 选择文献 (文库列表)...”按钮调起专用的多选清单弹窗：
1. **全库本地修复与清洗**（`LocalCleanDialog`）
2. **AI 快速理解 · 任务范围选择**（`TaskScopeDialog`，第 ③ 步执行范围）
3. **AI 校对核查 · 范围选择**（`TaskScopeDialog`，第 ④ 步校对范围）
4. **AI 深度归纳 · 范围选择**（`TaskScopeDialog`，第 ⑤ 步归纳范围）

上述四大入口统一复用了通用控件 **`DocSelectDialog`**（定义于 `app_core/ui/local_clean_dialog.py`）。此前主界面 `LibraryView` 已经实现了点击表头多维排序（升序 ▲ / 降序 ▼），但 `DocSelectDialog` 内的表格仍然处于未绑定排序点击的状态，导致用户面对数十乃至上百篇文献时，无法通过快速点击年份（倒序查看最新文献）、DOC-ID、标题字母或处理状态进行排序筛选，操作体验不够统一。

---

## 2. 根因定位与架构实现

### 2.1 架构复用分析
`DocSelectDialog` 采用左侧目录树（Collections Tree）+ 右侧表格列表（QTableWidget）的双栏排版：
- 列 0：`DOC-ID`（字段：`doc_id`）
- 列 1：`标题`（字段：`title`）
- 列 2：`类型`（字段：`doc_type`）
- 列 3：`年份`（字段：`pub_year`）
- 列 4：`期刊`（字段：`journal`）
- 列 5：`作者`（字段：`authors`）
- 列 6：`下载 / 解析状态`（字段：`download_status` 结合 `parse_status`）

### 2.2 核心改造方案

1. **表头交互与手势响应**：
   - 设置 `header_view.setSectionsClickable(True)` 并设置小手指针游标 `header_view.setCursor(Qt.PointingHandCursor)`；
   - 信号绑定：`header_view.sectionClicked.connect(self._on_header_section_clicked)`。

2. **智能初序与反转逻辑（与主文献库一致）**：
   - 初始状态：未指定排序列时保持数据库默认排序，表头无箭头；
   - **首次点击“年份”（`pub_year`）**：默认采用**降序（`DESC`）**，优先将最新发表文献排在顶部；
   - **首次点击其他列（DOC-ID、标题、类型、期刊等）**：默认采用**升序（`ASC`）**（按字典序排列）；
   - **再次点击当前排序列**：自动在升序（`ASC`）与降序（`DESC`）之间往复切换；
   - **视觉指示符**：当前排序列标题后附带 ` ▲` 或 ` ▼`，点击切换到其他列时自动清除旧列指示符，恢复纯文本。

3. **健壮的内存稳定排序引擎（`_apply_sort`）**：
   - 采用多键比较函数 `_sort_key`：
     - **年份（`pub_year`）**：严格进行整型数值比较（如 $2026 > 2015 > 1935$），缺失或非数值项在升序或降序下均稳健兜底置于列表末尾；
     - **作者（`authors`）**：利用 `_format_authors_short` 转换为人名简写后按小写字典序排序；
     - **状态（`download_status`）**：组合 `f"{dl} / {pr}"` 进行直观分类聚类；
     - **次级键（`doc_id`）**：加入次级键保证排序的确定性与稳定性（Stable Sorting）。

4. **无缝的选区与状态保持**：
   - `DocSelectDialog` 具有持久化选中集合 `self._selected_doc_ids`；
   - 表头排序触发 `_populate_table()` 重新渲染时，会严格比对每一行的 `doc_id` 并恢复 `setSelected(True)`；
   - 用户在排序前所勾选、框选或连选的文献，在排序后**100% 保持选中状态，零丢失、零混乱**。

5. **搜索与目录树联动继承**：
   - 在搜索框（`search_edit`）输入过滤关键词后，过滤结果依然实时遵循当前排序列与升降序方向；
   - 在左侧“我的文库”层级树中切换分类目录时，`_load_data()` 会自动带入当前排序规则，无缝保持用户的排序偏好。

---

## 3. 自动化测试与质量验证

在 `tests/test_v302.py` 中编写专项测试用例，覆盖全部场景：
1. `test_v302_version_json`：验证版本元数据正确晋升至 `v3.0.2`；
2. `test_doc_select_dialog_header_sort_click`：
   - 测试点击年份表头首次为 DESC（2026 $\rightarrow$ 2015 $\rightarrow$ 1935）；
   - 测试再次点击年份表头翻转为 ASC（1935 $\rightarrow$ 2015 $\rightarrow$ 2026）；
   - 测试点击标题表头切换为 ASC（Alpha $\rightarrow$ Beta $\rightarrow$ Gamma）并清除年份箭头；
   - 测试再次点击标题表头翻转为 DESC；
   - 测试点击 DOC-ID 切换排序；
3. `test_doc_select_dialog_sorting_preserves_selection`：
   - 测试多篇文献选中状态在经历多次表头排序点击后依然精准保留；
4. `test_doc_select_dialog_search_retains_sort`：
   - 测试在已设定排序状态下进行模糊搜索，过滤后的子集依然保持既定排序方向，清空搜索框后恢复全量排序。

**测试执行结果**：全工程 **226 个测试用例全部通过（100% Passed，耗时 18.91s）**。

---

## 4. 交付清单与文件变动

1. **版本配置**：
   - `app_core/version.json`：提升至 `3.0.2`（Build `20261118`）；
2. **核心业务代码**：
   - `app_core/ui/local_clean_dialog.py`：
     - 新增 `_DOC_SELECT_SORT_FIELDS` 与 `_DOC_SELECT_BASE_HEADER_LABELS`；
     - `DocSelectDialog` 新增 `sort_column_index`、`sort_order`；
     - 表头接入点击信号、游标样式、交互式排序；
     - 实现 `_on_header_section_clicked`、`_update_header_indicators`、`_get_current_order_by`、`_apply_sort`；
     - 优化 `_load_data` 与 `_apply_filter` 继承排序。
3. **测试代码**：
   - `tests/test_v302.py`（新增 4 组完整测试，100% 通过）；
4. **运行环境同步**：
   - 已同步最新代码至本地安装目录 `C:\Normal Program Files\AtomicCalculationDB\app_core\` 及 `dist\app_core\`；
5. **本地打包产物（遵循 Z 补丁规范，未生成 Full、未修改 README、未发 Release）**：
   - `release/AtomicCalculationDB_Patch_v3.0.2.zip`
   - `release/AtomicCalculationDB_Lite_v3.0.2.zip`
