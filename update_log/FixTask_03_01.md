# FixTask_03_01.md — `AtomicCalculationDB` (V2.2.1) 幽灵弹窗消除、Chrome 级窗口控制与拖拽还原、卡片网格等高对齐、卡片手动/AI 修改与提取净化任务书

> **致 AI Coding Agent 的全局执行说明**：
> 在 V2.2 实际运行测试中，发现了以下需要立即修复的 UI 交互 Bug 与功能增强需求：
> 1. 点击卡片切换详情抽屉时闪烁空白系统弹窗；
> 2. 右上角最小化/最大化/关闭按钮使用了失调的字符而非 Chrome 风格等高矢量图标，且最大化状态下按住顶部拖动未自动还原为窗口模式；
> 3. 探索中心网格第一张卡片高度跨两行、右侧抽屉与黄色横幅卡片局部裁切；
> 4. 新增知识卡片的【手动编辑】、【AI 按修改意见重修】与【删除卡片】功能；
> 5. 拦截“证据截断”废卡生成并净化公式字段。
> 请严格按以下 6 项任务完成修复并重新构建 `AtomicCalculationDB.exe`。

---

## Task 1：彻底根除点击卡片或刷新界面时一闪而过的“空白幽灵弹窗” Bug

* **问题根因**：
  1. 在 PySide6/Qt 中，当清空布局（如刷新右侧详情抽屉、关键词流式布局或卡片网格）时，若对尚处于可见状态的子控件直接调用 `widget.setParent(None)`，Qt 会将该无父级控件瞬间提升为独立的 OS 顶层窗口弹出，直到 `deleteLater()` 在下一事件循环将其销毁，从而产生一闪而过的空白弹窗！
  2. 此外，若新建子控件在加入父布局（`layout.addWidget(w)`）之前调用了 `w.show()`，或 `matplotlib` 未严格使用纯内存 `FigureCanvasAgg`，也会触发独立窗口闪烁。
* **修复要求**：
  1. 全局排查 `app_core/ui/` 下所有布局清理函数（尤其是详情抽屉刷新、卡片列表刷新、关键词云刷新）：在移除子控件时，**必须先调用 `widget.hide()`**，再执行 `layout.removeWidget(widget)` 与 `widget.deleteLater()`（严禁对可见控件裸调 `setParent(None)`）。
  2. 新建任何子控件时，必须传入 `parent` 或在 `layout.addWidget(child)` 之后由父容器统一管理显示，严禁对无父控件调用 `.show()`。
  3. 确保 `formula_renderer.py` 顶部强制声明 `matplotlib.use("Agg")`，且全程仅使用 `Figure()` + `FigureCanvasAgg(fig)` 离屏渲染，严禁调用任何 `matplotlib.pyplot` 窗口接口。

---

## Task 2：重写右上角窗口控制按钮（1:1 复刻 Google Chrome 矢量图标）与“最大化按住顶部拖动自动还原窗口”逻辑 (`main_window.py`)

### 2.1 抛弃字符按钮，改用 Chrome 标准 `10×10px` 矢量几何绘制 (`QPainter` / `QIcon` SVG)
* **问题表现**：当前最小化、最大化、向下还原、关闭按钮使用了 Unicode 文本字符（`—`, `□`, `❐`, `✕`），导致最大化方框极小、向下还原符号怪异，与 Google Chrome 差距明显。
* **修复要求**：自定义 `WindowControlButton(QPushButton)`，固定按钮尺寸（如 `46×36px`），彻底清空文本，使用 `QPainter` 开启抗锯齿或加载内联 SVG，以 `1.2px` 线宽、统一 `10×10px` 视觉框标准绘制以下 4 个图标：
  1. **最小化 (`Minimize`)**：居中绘制长 `10px` 的水平直线（`y = center_y, x1 = center_x - 5, x2 = center_x + 5`）。
  2. **最大化 (`Maximize`)**：居中绘制 **`10×10px` 空心正方形**（与关闭按钮 `✕` 的视觉高度完全一致，绝不能缩水变小！）。
  3. **向下还原 (`Restore Down`)**：严格对齐 Chrome 样式——由左下方前景空心正方形（`8×8px`）与右上方露出的后景折线框（相距 `2px` 的 `L` 形上边与右边折线）组合而成，严禁使用实心阴影字符 `❐`。
  4. **关闭 (`Close`)**：居中绘制 `10×10px` 的对角交叉线 `✕`；鼠标悬停（Hover）时按钮背景变为 `#E81123`（正红），图标线条变为纯白 `#FFFFFF`；其余三个按钮悬停时背景变为柔和半透明浅蓝灰 `rgba(11, 87, 208, 0.08)`。

### 2.2 修复最大化（全屏）状态下按住顶部拖动未还原为窗口模式的 Bug
* **问题表现**：当前在最大化状态下按住顶部标题栏拖动时，窗口没有退出最大化变为普通窗口，而是直接把整个全屏大小的窗口拖偏了，且右上角的“向下还原”图标也没有变回“最大化”单方框图标。
* **修复要求**：
  1. **统一最大化状态管理与图标同步**：
     * 无论是点击最大化按钮、双击标题栏，还是通过系统快捷键改变窗口状态，重写 `changeEvent(self, event)` 监听 `QEvent.Type.WindowStateChange`（若使用自定义最大化标志 `_is_maximized` 则封装统一的 `set_maximized_state(bool)` 方法）：
     * 当处于**最大化状态**时：右上角中间按钮图标强制切换为 **【向下还原（双叠框）】**，主窗口外层圆角设为 `0px`（严丝合缝贴合屏幕四角，不留边距）；
     * 当处于**普通窗口状态**时：右上角中间按钮图标强制切换为 **【最大化（`10×10px` 单方框）】**，主窗口恢复圆角样式。
  2. **最大化状态下按住顶部拖动自动还原（Drag-to-Restore）**：
     * 在标题栏的 `mouseMoveEvent` 中，当检测到鼠标左键按下并拖动距离超过阈值（`4px`）时：
     * 若当前处于最大化状态（`self.isMaximized()` 或 `self._is_maximized` 为真）：
       1. 先计算鼠标当前在最大化窗口宽度上的水平比例：`ratio_x = event.position().x() / max(1, self.width())`；
       2. **立即退出最大化状态，恢复为普通窗口尺寸**（`self.showNormal()` 或恢复保存的 `_normal_geometry` 尺寸，并将右上角图标立即刷新为【最大化单方框】！）；
       3. 将恢复后的普通窗口位置定位到鼠标正下方，使鼠标保持在标题栏相同的相对比例位置：
          ```python
          new_x = int(global_pos.x() - self.width() * ratio_x)
          new_y = int(global_pos.y() - self._press_local_pos.y())
          self.move(new_x, new_y)
          self._drag_offset = global_pos - self.pos()
          ```
       4. 随后继续跟随鼠标平滑移动普通窗口。
  3. **支持双击标题栏切换最大化/还原**：在标题栏 `mouseDoubleClickEvent` 中切换最大化与还原状态。

---

## Task 3：修复探索中心卡片网格高度失控、横幅卡片裁切与右侧抽屉水平溢出

1. **修复探索中心卡片网格高度不一致问题（左上角第一张卡片跨两行高）**：
   * 所有探索中心网格中的 L2 知识卡片外框必须采用**统一的固定高度**（如 `setFixedHeight(245)`，水平策略 `QSizePolicy.Policy.Expanding`，垂直策略 `QSizePolicy.Policy.Fixed`）。
   * 在 `QGridLayout` 中添加卡片时，严格按 `row = index // cols, col = index % cols` 排列，`rowSpan` 与 `colSpan` 必须恒为 `1`，单元格对齐方式设为 `Qt.AlignmentFlag.AlignTop`，并在网格底部设置 `setRowStretch(last_row + 1, 1)`，确保卡片绝不纵向拉伸变形。
2. **修复黄色提示/争议卡片及右侧详情抽屉的边缘裁切问题**：
   * 检查黄色横幅卡片列表的容器高度，移除过小的固定高度限制，确保卡片右侧的 `[查看]` 按钮完整居中显示，不被底部裁切。
   * 在右侧【知识卡详情】抽屉的 `QScrollArea` 中，强制设置 `setWidgetResizable(True)` 且 `setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)`，内部容器右侧预留 `24px` 安全内边距，确保右侧文字与“复制 LaTeX”按钮 100% 完整显示在抽屉内部。

---

## Task 4：新增知识卡片【手动编辑】、【AI 按修改意见重修】与【删除卡片】功能

在右侧【知识卡详情】抽屉顶部标题栏下方新增操作按钮栏：**`[✏️ 手动编辑]`** | **`[✨ AI 修改]`** | **`[🗑️ 删除卡片]`**

1. **直接手动编辑 (`[✏️ 手动编辑]`)**：
   * 点击后允许用户直接修改卡片的 `标题 (title_zh)`、`类型 (card_type)`、`四维物理坐标`、`核心结论 (summary_zh)`、`前提与边界 (premises_and_conditions)`、`核心公式 LaTeX (formulas_latex)`、`代码与参数 (code_snippet)` 与 `踩坑预警 (pitfall_warning)`。
   * 点击 **`[💾 保存修改]`** 后：自动重新构建 `search_payload`，同步更新 `SQLite (knowledge_cards + knowledge_fts)`，重新计算该卡片的 `BGE-M3` 向量更新至 `Qdrant`，并立即刷新当前详情抽屉与左侧卡片列表。
2. **提出修改建议让 AI 自动修正 (`[✨ AI 修改]`)**：
   * 点击后展开输入框（提示：*“请输入修改建议，例如：补全公式定义、修正某参数或删除乱码描述...”*）+ **`[🚀 发送给 AI 重新提炼]`** 按钮。
   * 将 `[当前卡片 JSON + 关联的 L3 原始证据块全文 + 用户的修改指令]` 发送给 `FastLLM`，获取修正后的 JSON 卡片并覆盖保存（同步更新 SQLite/FTS5/Qdrant 及文献 Token 消耗统计）。
3. **删除无效卡片 (`[🗑️ 删除卡片]`)**：
   * 确认后从 `SQLite` 及 `Qdrant` 向量库中彻底删除该卡片，并立即刷新列表。

---

## Task 5：净化卡片提取质量（拦截“证据截断”废卡 & 净化公式字段）

1. **拦截“证据截断/信息不足”废卡生成**：
   * 在 `atomic_prompts.py` 中明确规定：若当前片段信息残缺或无法提炼出明确物理结论，直接返回 `{"skip": true}`，严禁生成标题带有“（证据截断）”或内容为“无法可靠重建具体计算策略”的废卡。
   * 在启动数据清洗钩子中，自动删除当前数据库中标题含有 `证据截断` 或正文声明 `无法可靠重建具体计算策略` 的无效卡片。
2. **净化 `formulas_latex` 公式字段与 PDF 伪影过滤**：
   * 过滤老 PDF 根号/公式私有字体映射产生的 `vuuut1` 等伪影。
   * 强制约束 `formulas_latex` 字段只允许包含纯数学公式 LaTeX 表达式，严禁将英文解释长句（如 `"The focus fragment contains..."`）塞入公式字段；若含解释文字则自动剥离至正文中。

---

## Task 6：验证与重新打包 `AtomicCalculationDB.exe`

1. 运行 `pytest` 验证：
   * 窗口最大化/还原状态切换、顶部拖动自动退出最大化还原为普通窗口及按钮图标同步测试通过；
   * 切换卡片详情零空白幽灵窗口闪烁测试通过；
   * 卡片手动修改、AI 建议修改、删除卡片及废卡自动清理测试通过。
2. 重新编译生成 **`AtomicCalculationDB.exe`** 并更新 `docs/` 截图。