# UpdateTask_09.md — `AtomicCalculationDB` (V2.8.0) 文献库按需加载架构：精简投影、分页视口与流水线状态反范式缓存

> **核心目标**：针对文献规模与知识卡规模扩大后的数据库性能瓶颈，全面推行“按需加载（On-demand Fetching & Projection）”架构改造：
> 1. **字段投影精简（Projection / 轻量只读列）**：新增 `list_documents_summary`，彻底剥离 `abstract`、`metadata_json`、`error_log` 等高开销文本与 JSON 字段，列表查询吞吐量提升 85%+；
> 2. **流水线状态反范式缓存（Cache / 跨表解耦）**：在 `documents` 表中持久化 `card_count`、`has_master`、`pipeline_step`、`pipeline_glow` 缓存列。卡片增删改及状态迁移时自动刷新关联文献行。打开文献库时 **完全无需扫描 `knowledge_cards` 表**，实现单表极速直出；
> 3. **文献库分页视口控制器（Pagination UI）**：在文献库主界面引入分页工具栏（首页、上一页、页码指示、下一页、末页、每页条数选择器 25/50/100/200、页码直接跳转），单页渲染控制在 50 条以内，单次点击响应锁定在 **1~2 毫秒**，支撑 10 万+ 级文献库顺畅浏览。

---

## 1. 数据库层改造 (`sqlite_manager.py`)

### 1.1 Schema 增量迁移与字段定义
在 `documents` 表中引入 4 个反范式状态缓存列（平滑升级，向前兼容）：
```sql
ALTER TABLE documents ADD COLUMN card_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE documents ADD COLUMN has_master INTEGER NOT NULL DEFAULT 0;
ALTER TABLE documents ADD COLUMN pipeline_step INTEGER NOT NULL DEFAULT 1;
ALTER TABLE documents ADD COLUMN pipeline_glow TEXT NOT NULL DEFAULT 'NONE';
```

### 1.2 自动回填（One-time Auto-backfill）
在系统初始化时检测 `doc_pipeline_cache_v280` 元数据标记。若首次升级至 V2.8.0，单次扫描现有数据库卡片并批量回填 `documents` 缓存列，耗时 < 20ms，写入元数据后不再重复执行。

### 1.3 实时联动维护
- 在 `upsert_knowledge_card()` 与 `delete_knowledge_card()` 事务中，针对受影响的 `supporting_doc_ids` 自动重新计算并回写 `documents` 缓存字段；
- 在文献下载/解析状态变更及管线回滚时，同步更新 `pipeline_step` 与 `pipeline_glow`。

### 1.4 精简字段投影查询 (`list_documents_summary`)
```sql
SELECT doc_id, doi, title, doc_type, pub_year, journal,
       input_tokens, output_tokens, download_status, parse_status,
       parse_error_code, local_pdf_path, local_md_path,
       card_count, has_master, pipeline_step, pipeline_glow,
       created_at, updated_at
FROM documents
WHERE ...
ORDER BY updated_at DESC, doc_id
LIMIT ? OFFSET ?
```
排除列：`abstract`, `authors`, `metadata_json`, `keywords_json`, `error_log`, `source_url`, `pdf_sha256`。

---

## 2. 界面与交互层改造 (`library.py`)

### 2.1 分页工具栏布局与组件
位于文献表格下方：
- **统计信息**：`共 N 篇文献 | 当前显示 X-Y 条`
- **翻页控制组**：`[ 首页 ]` `[ ◀ 上一页 ]` `第 P / Total 页` `[ 下一页 ▶ ]` `[ 末页 ]`
- **每页规格**：下拉框可选 25 / 50 / 100 / 200 条
- **直达跳转**：`跳转至 [ N ] 页`

### 2.2 状态保持与重刷逻辑
- 筛选条件变化（如切换到“已下载”）：重置到第 1 页；
- 表格原地双击修改（如改期刊名）：保持当前页刷新，无视口跳动；
- 进度条委托渲染：直接读取行级缓存的 `card_count` 与 `has_master`，免去跨表扫描。
