# FixTask_07_01.md — 数据治理 P0（schema v3.1）

## 1. 目标

按数据治理 review 的 P0 顺序，先处理四件会直接损坏知识或让索引静默失效的事情：

1. 相似卡片不再直接吞并，改为非破坏性关系；
2. Qdrant 更新失败可重试，`needs_vector_update` 不再只写不消费；
3. 扫描件与程序故障分开，增加 `NEEDS_OCR` 语义；
4. 建立 10–20 篇文献的人工评估集骨架。

本轮不改动 P1 的结构化适用条件、逐块证据定位、HTML 表格统一模型和图表通道。

## 2. Schema v3.1

`app_core/db/schema_v2.sql`：

- `SCHEMA_VERSION` 从 `3.0` 升到 `3.1`；
- 新增 `card_relations`：
  - `relation_id`
  - `source_knw_id` / `target_knw_id`
  - `relation_type`：`SAME_CLAIM / SUPPORTS / CONTRADICTS / RELATED`
  - `confidence`、`reason`、`evidence_json`
  - 唯一约束 `(source_knw_id, target_knw_id, relation_type)`
  - 源/目标索引，删除卡片时级联删除关系；
- `documents` 新增 `parse_error_code TEXT NOT NULL DEFAULT ''`；
- 旧数据库通过 `_ensure_columns()` 和 `CREATE TABLE IF NOT EXISTS` 自动迁移。

## 3. 非破坏性卡片关系

`pipeline._store_draft()` 不再把新卡合并进旧卡：

- 新提取卡始终写入数据库；
- 相似命中时写入 `SAME_CLAIM` 关系，保留两套卡片与两套 L3 证据；
- 冲突命中时写入 `CONTRADICTS` 关系；
- 旧卡仍更新 `consensus_count`、`supporting_doc_ids`、`has_conflict`；
- 卡片详情新增“关联卡片”区块，显示关系类型、对方卡片和相似度。

已知限制：

- 历史数据库中已经被合并掉的卡片无法自动恢复；本改动只保证从今以后不再丢失；
- 关系表暂未提供“全部关系”独立页面，只在卡片详情展示；
- `SUPPORTS / RELATED` 目前只预留类型，尚未自动生成。

## 4. 向量索引重试

新增 `VECTORIZE` 任务：

- 卡片写入、人工修改、审计回写后，如果同步向量化失败，自动入队；
- `tasks.scheduled_for` 参与 `claim_next_task()` 过滤，支持延迟重试；
- 失败后按 10s、20s、40s、80s、160s 退避，最多 5 次；
- 成功后 `mark_vectorized(..., True)` 同时清除 `needs_vector_update`；
- 审计恢复卡片也会重新入队；
- Qdrant 不可用时保留 `needs_vector_update=1`，不会静默丢失。

## 5. NEEDS_OCR 语义

- 扫描件文本层过少时，仍保持 `parse_status=FAILED` 兼容现有筛选；
- 同时写入 `parse_error_code=NEEDS_OCR`；
- 文献库解析状态显示为“需要 OCR”，使用橙色而不是红色；
- 其他解析异常写入 `PARSE_ERROR`，缺少源文件写入 `MISSING_SOURCE`；
- 重新挂载 PDF 时清除旧的 `parse_error_code`。

## 6. 评估集骨架

新增：

- `eval/README.md`：覆盖类别、标注字段、指标、成本口径和首轮流程；
- `eval/cases.example.jsonl`：3 条示例记录，复制为 `cases.jsonl` 后填写真实 DOC-ID。

指标包括：重要知识点召回、前提完整率、证据支持率、数值/单位准确率、误合并率、冲突误报/漏报、策略验证通过率、每条有效知识成本。

## 7. 测试

新增 `tests/test_v31.py`：

- schema v3.1 表/列存在；
- 关系表双向查询；
- 同义卡片跨文献保留为两张卡并写入 `SAME_CLAIM`；
- 向量失败生成带 `scheduled_for` 的重试任务；
- `claim_next_task()` 尊重 `scheduled_for`；
- v3.0 数据库重新打开后自动补齐 v3.1 对象。

更新：

- `tests/test_v2_core.py` schema 版本断言 3.0 → 3.1；
- `tests/test_v223.py` 扫描件断言 `parse_error_code == "NEEDS_OCR"`。

全量回归：**145 passed**。

## 8. 未做 / 下一轮

- 结构化适用条件（目标量、单位、不确定度、态/跃迁、收敛判据）；
- 逐块证据定位（block_id、字符范围、bbox、表格行列/表头/脚注）；
- HTML/MinerU 表格统一 `TableBlock`；
- 图注与 `FigureBlock`；
- 处理台账；
- L1 总纲 stale 传播；
- LLM 原始响应归档；
- 计算策略执行与验证闭环。

本轮未构建发布包，也未覆盖安装目录。
