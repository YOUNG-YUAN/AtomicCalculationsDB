# UpdateTask_10.md — `AtomicCalculationDB` (V2.9.0) 本地可控维护弹窗、Zotero 式层级分类目录树与全列表头点击排序

> **核心目标**：针对科研人员在文献快速增长下的管理、分类、检索与局部维护需求，完成三大系统级交互与底层架构重构：
> 1. **本地维护与清洗可控对话框（Local Clean Dialog & Safe Interruption）**：
>    - 替代原有一键盲目全跑的模式，改为配置对话框：支持工序勾选（修复元数据、清理废卡、公式净化、聚类合并、争议校验）、范围选择（选中项/全库）、并提供“全选”与“清空”快捷操作；
>    - 引入基于**单项微事务（Per-item Micro-transaction）与检查点（Safe Checkpoints）**的安全中断控制，用户点击停止后优雅退出，确保已修复数据落盘、未处理数据完好，绝对不损坏数据库。
> 2. **Zotero 式层级分类目录树（Hierarchical Collections Tree）**：
>    - 在 SQLite 中新增 `collections` 与 `collection_documents` 关系表，支持多层嵌套目录；
>    - 在文献库左侧引入可伸缩拖拽的目录树面板（`QSplitter`），预设「全部文献」、「最近添加」、「未分类文献」及用户自定义树状文件夹；
>    - 支持目录右键新建、重命名、删除，以及文献列表项右键一键「归档到分类」或「从当前分类移除」。
> 3. **表头点击全列自由排序（Clickable Column Header Sorting）**：
>    - 文献列表表头全面支持点击正反序排序，包括 DOC-ID、标题、年份、期刊、Token 消耗、下载状态、解析状态、以及最重要的**流程进度（按工序 1~5 与金/绿光芒排序）**；
>    - 表头呈现动态排序箭头（▲/▼），结合底层索引与 V2.8.0 的精简投影分页，实现万级文献下的毫秒级即时排序。

---

## 1. 数据库层改造 (`schema_v2.sql` & `sqlite_manager.py`)

### 1.1 分类目录表结构
```sql
CREATE TABLE IF NOT EXISTS collections (
    collection_id TEXT PRIMARY KEY,
    parent_id     TEXT,
    name          TEXT NOT NULL,
    icon          TEXT NOT NULL DEFAULT 'folder',
    sort_order    INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (parent_id) REFERENCES collections(collection_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS collection_documents (
    collection_id TEXT NOT NULL,
    doc_id        TEXT NOT NULL,
    added_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (collection_id, doc_id),
    FOREIGN KEY (collection_id) REFERENCES collections(collection_id) ON DELETE CASCADE,
    FOREIGN KEY (doc_id) REFERENCES documents(doc_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_coll_docs_doc ON collection_documents(doc_id);
CREATE INDEX IF NOT EXISTS idx_coll_parent ON collections(parent_id);
```

### 1.2 目录管理与过滤 API
- `create_collection(name, parent_id=None, icon='folder') -> str`
- `rename_collection(collection_id, new_name) -> bool`
- `delete_collection(collection_id) -> bool`
- `list_collections() -> list[dict]`
- `add_documents_to_collection(collection_id, doc_ids) -> int`
- `remove_documents_from_collection(collection_id, doc_ids) -> int`
- `get_document_collection_ids(doc_id) -> list[str]`
- `count_collection_documents(collection_id) -> int`
- `list_documents_summary(...)` 与 `count_documents(...)` 支持 `collection_id` 参数。

---

## 2. 维护管理层改造 (`reprocess.py` & `local_clean_dialog.py`)

- `ReprocessManager.local_clean` 支持传入：
  - `operations`: 允许只运行某些阶段（如仅 `{"metadata", "purge"}`）；
  - `doc_ids`: 允许针对指定文献列表执行；
  - `should_stop`: 每一小项处理前检查中断信号；单项提交事务保证中断零损坏。
- `LocalCleanDialog`：
  - 提供工序复选框与全选/全不选按钮；
  - 提供范围选择单选钮（选中文献 / 筛选结果 / 全库）；
  - 实时进度条与「⏹ 立即安全中止」按钮。

---

## 3. 界面与交互层改造 (`library.py`)

- 左右分栏面板（`QSplitter`）：
  - 左侧：目录树 `QTreeWidget`，带节点计数和右键快捷菜单；
  - 右侧：原文献列表与操作工具栏。
- 表头排序交互：
  - `horizontalHeader().sectionClicked` 触发排序；
  - 维护当前排序字段与顺序（ASC/DESC），自动刷新当前分页。

---

## 4. 验证与交付记录

- **测试套件**: 181 个测试全部通过 (`python -m pytest` 181 passed in 26.35s)。
- **新建测试**: `tests/test_v290.py`（覆盖目录层级增删改查、局部维护工序与中断信号拦截、表头正反序点击切换）。
- **更新补丁包**: `release/AtomicCalculationDB_Patch_v2.9.0.zip` (512.6 KiB, `testzip()` VALID)。
- **完整发布包**: `release/AtomicCalculationDB_Full_v2.9.0.zip` (1635.1 MB, 9968 个文件, `testzip()` VALID)。
- **知识图谱索引**: `codebase-memory-mcp` 全量重新索引完成（3039 nodes, 15497 edges）。
