# FixTask_10_01.md — 新建分类防闪退、自由拖拽调整列宽与横向平滑滚动条支持

## 1. 现象与问题反馈

在 V2.9.0 引入 Zotero 式文献目录树与表头排序功能后，用户在实际使用中反馈了两大高频体验与稳定性问题：

1. **新建分类偶发闪退崩溃**：
   > “有时候新建分类会闪退。”
   - 点击“+ 新建分类”或在左侧目录树右键弹出菜单中点击“新建子分类/顶层分类”时，软件在特定情况下直接闪退崩溃并退出。

2. **表格右侧列截断且无法调整列宽**：
   > “而且列表显示不全，我建议你增加一个横向的滚动翻页条。然后允许拖动表头的分界线来改变列宽。作为一个FixTask更新吧”
   - 用户在较窄窗口、笔记本屏幕或拖宽左侧分类树时，表格右侧的“解析”列被切断、第 9 列“流程进度”（五步圆圈指示器）完全溢出到可视区域外不可见，且界面没有任何横向滚动条；
   - 鼠标悬浮在表头两列分界线处无法拖动改变列宽（没有出现 `<-||->` 调整光标）。

---

## 2. 根因深度剖析

### 2.1 新建分类闪退根因
经调试与 Qt 事件生命周期排查，定位出四大崩溃诱因：
1. **Qt `clicked` 信号布尔形参隐式传递**：
   原连接方式为 `self.btn_new_collection.clicked.connect(self._create_new_collection_prompt)`。Qt 的 `QPushButton.clicked` 会默认发出 `(bool checked=False)` 信号，导致 `_create_new_collection_prompt(self, parent_id=None, ...)` 接收到的首个参数为布尔值 `parent_id = False`，进而传递给数据库与树节点逻辑，引发类型混乱与潜在的外键错误；
2. **上下文菜单事件中同步销毁 C++ 节点引发 Use-After-Free**：
   在左侧目录树上右键点击“📁 新建子分类”时，`QMenu.exec()` 模态循环正在处理当前被点击的 `QTreeWidgetItem`。当用户输入名称确认后，代码在回调内直接同步调用了 `_reload_collections_tree()` $\rightarrow$ `self.collections_tree.clear()`。这立即销毁了当前 C++ 树节点对象，而菜单事件与视口事件尚未完成出栈，导致 Qt C++ 底层访问悬空野指针，触发段错误（Segmentation Fault）闪退；
3. **未捕获的 Python 异常冒泡至 Qt 事件循环**：
   `_create_new_collection_prompt`、`_rename_collection_prompt` 与 `_delete_collection_prompt` 缺乏外层 `try...except` 保护与顶层窗口绑定（`self.window()`），一旦发生数据库锁争用或异常，异常直接冲垮 Qt 事件主循环导致进程自杀；
4. **`QTreeWidgetItemIterator` 迭代器悬空风险**：
   在目录树重绘与重新选中时使用 C++ 迭代器 `QTreeWidgetItemIterator`，在频繁清空与增删树节点时存在迭代器失效风险。

### 2.2 表格无法拖动列宽与右侧列截断根因
1. **表头被硬编码为 `QHeaderView.Fixed` 与 `Stretch`**：
   在 `app_core/ui/views/library.py` 中将表头各列设置为了 `Fixed` 模式，Qt 规范明确规定：`Fixed` 模式强制禁用用户拖动改变列宽的手柄交互；而第 1 列硬编码为 `Stretch` 导致其被视口宽度强制限制；
2. **缺少横向滚动条策略与像素滚动模式**：
   表格未启用 `setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)`，导致当所有列的最小/默认宽度之和（约 1385px）大于右侧面板宽度时，右侧列被生硬裁切抛弃，且无法横向翻页滚动。

---

## 3. 修复方案与具体实现

### 3.1 目录分类全流程崩溃免疫重构
在 `app_core/ui/views/library.py` 中进行了全方位的健壮性加固：
1. **信号解耦与参数清洗**：
   - 将按钮信号改为 `lambda: self._create_new_collection_prompt()`，阻断 `checked: bool` 混入；
   - 在 `_create_new_collection_prompt` 入口进行强类型校验：
     ```python
     if not isinstance(parent_id, str):
         parent_id = None
     if not isinstance(parent_name, str):
         parent_name = None
     ```
2. **异步安全重载避免 Use-After-Free**：
   - 分类创建、重命名、删除及文献归档后，树重载全部采用 `QTimer.singleShot(0, self._reload_collections_tree)` 调度至下一个事件循环周期，确保当前上下文菜单与鼠标事件彻底退栈释放后再清空与重建树，彻底消除 Qt C++ 内存崩溃；
3. **安全纯 Python 节点检索与信号守护**：
   - 彻底废除 `QTreeWidgetItemIterator`，新增无指针风险的纯 Python 遍历方法 `_find_tree_item(cid)`；
   - 重构 `_reload_collections_tree`，使用 `try...finally: self.collections_tree.blockSignals(False)` 确保异常时不会永久阻塞信号；
4. **异常全量拦截与友好提示**：
   - 将分类的新建、重命名、删除、归档及移除操作全量包裹在 `try...except Exception as exc: logger.exception(...); QMessageBox.warning(...)` 中，并使用 `self.window() or self` 确保对话框正确定位在顶层窗口；
5. **新建后自动高亮与即时刷新**：
   - 新建分类成功后，自动将 `self.current_collection_id` 设为新创建的 `col_id`，用户界面立刻定位并选中新建的分类目录。

### 3.2 交互式表头列宽调整与平滑横向滚动条
1. **全列启用交互式列宽拖拽 (`QHeaderView.Interactive`)**：
   ```python
   header_view.setSectionResizeMode(QHeaderView.Interactive)
   header_view.setSectionsMovable(False)
   ```
   - 鼠标悬浮在任意两列表头分割线时，光标立即变为 `<-||->`（双向拉伸光标），用户可随心所欲自由拖动调整每一列的宽度；
   - 锁定列移动（`setSectionsMovable(False)`），避免拖拽调整列宽时误触发列拖拽换位。
2. **启用像素级平滑横向滚动条**：
   ```python
   self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
   self.table.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
   self.table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
   self.table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
   ```
   - 当屏幕较小、窗口缩小时，表格底部自动平滑浮现横向滚动条；
   - 支持滚轮平滑滚动与拖动滚动条，确保“下载”、“解析”、“流程进度”等所有列 100% 完整可见。
3. **人性化初始列宽配置**：
   - DOC-ID: 190px
   - 标题: 280px
   - 类型: 60px
   - 年份: 65px
   - 期刊: 150px
   - 作者: 140px
   - Token 消耗: 170px
   - 下载: 95px
   - 解析: 95px
   - 流程进度: 140px
4. **分栏条防完全折叠保护**：
   - `self.splitter.setChildrenCollapsible(False)`，防止左右分栏被意外拖拽至 0 宽度折叠隐藏。

---

## 4. 验证与回归测试

1. **自动化测试套件全量通过**：
   - 运行全部 181 个测试（`pytest`），包含核心库、解析器、爬虫、流水线、V2.9.0 目录与排序、以及 V2.9.1 专项防崩与交互测试，全部通过（**181 passed in 23.96s**）。
2. **V2.9.1 专项交互与防闪退单元测试**：
   - 检验表头各列的 `sectionResizeMode` 均为 `QHeaderView.Interactive`；
   - 检验横向滚动条策略为 `Qt.ScrollBarAsNeeded`，滚动模式为 `ScrollPerPixel`；
   - 模拟 `QPushButton.clicked(False)` 信号传入，验证新建顶层分类和新建子分类均安全完成且正确归属。
3. **打包分发与完整性校验**：
   - 版本号提升至 **`2.9.1`**（`app_core/version.json`）；
   - 同步至 `dist/app_core/`；
   - 增量更新补丁包：`release/AtomicCalculationDB_Patch_v2.9.1.zip`（513.2 KiB）；
   - 完整便携安装包：`release/AtomicCalculationDB_Full_v2.9.1.zip`（1635.1 MB）；
   - 经 Python `zipfile.testzip()` 严格 CRC 校验，两份安装包均完美无损。
