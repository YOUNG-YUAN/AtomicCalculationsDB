# FixTask_05_01.md — `AtomicCalculationDB` (V2.4.0) 跨模型知识卡审计实现记录

## 1. 目标

按 `UpdateTask_05.md` 实现：

- 导出包含一篇文献全部知识卡的 Markdown 审计包；
- 提供严格 JSON 审计提示词；
- 导入外部 AI 的修改/删除/新增 JSON；
- 不调用任何大模型、不消耗 Token；
- 审计删除只软隐藏，不物理删除；
- 记录生成模型来源、审计历史、多 AI 评价与分歧；
- 审计后确定性重算冲突/共识。

## 2. 已实现

### 2.1 数据库 schema v3

- `SCHEMA_VERSION = 3.0`。
- `knowledge_cards` 新增：
  - `content_hash`
  - `source_kind`
  - `audit_status`
  - `audit_delete_suggested`
  - `audit_hidden`
  - `audit_delete_reason`
  - `audit_delete_by`
  - `audit_delete_at`
  - `audit_review_count`
  - `audit_positive_count`
  - `audit_negative_count`
  - `audit_score_min`
  - `audit_score_max`
  - `audit_divergence`
  - `needs_vector_update`
  - `master_stale`
- 新增表：
  - `card_model_provenance`
  - `card_audit_reviews`
  - `card_revisions`
  - `document_audit_exports`
  - `audit_import_batches`
- 旧库迁移时先补列，再创建审计索引；3,882 张卡的内容指纹自动回填。

### 2.2 内容指纹与模型来源

- `db/card_hash.py` 提供规范化内容 SHA256。
- `models/provenance.py` 快照五类角色：
  - 快速提取
  - 深度归纳
  - 图像理解
  - 向量模型
  - 重排模型
- `pipeline._store_draft` 新建卡片时写入来源；`pipeline.revise_card` 更新来源。
- 不记录 API Key、Cookie 或代理密码。

### 2.3 审计导出

- `audit/report.py`：
  - 导出 `report.md` / `report_part-*.md`
  - `audit_prompt.md` / `audit_prompt_part-*.md`
  - `cards.jsonl`
  - `manifest.json`
  - `README.txt`
- 报告覆盖该文献全部卡片，包括建议删除卡片。
- 超过卡片数/字符阈值自动分片。
- 报告默认不输出已有审计评分，避免外部 AI 被锚定；UI 提供“报告包含已有审计”开关。
- 报告头写入 `report_id`、`report_hash`、`document_id`、`batch_id`。

### 2.4 审计导入

- `audit/importer.py`：
  - 解析 JSON；`.md` 可提取 fenced JSON；
  - 校验 schema、文献、报告 hash、卡片 ID、内容指纹、分数与动作；
  - 过期卡片标记 `STALE`，不自动覆盖；
  - `keep` / `modify` / `delete` / `new_cards`；
  - 修改写入 `card_revisions`；
  - 删除只设置 `audit_hidden=1` / `SUGGESTED_DELETE`；
  - 新卡片标记 `source_kind=AI_AUDIT_ADDED`；
  - 用户已确认卡片的修改/删除要求人工复核；
  - 导入完成后确定性重算共识和冲突；
  - 不调用 LLM，不触发云端 embedding；只标记 `needs_vector_update`。

### 2.5 现有卡片来源补录

- 新增 `scripts/backfill_provenance.py`，可为历史卡片批量补写指定角色来源。
- 已对安装目录数据库执行：
  - 模型：`deepseek-v4.1-flash-1017`
  - 节点：`DeepSeek API`
  - Provider：`api`
  - 角色：`fast_llm`（卡片生成来源）
  - 3,882 张卡片全部补齐，0 张遗漏。
- 执行前备份：`workspace/atomic_meta_pre_v24_provenance_20261106.sqlite`。
- 本次只补卡片生成来源；向量模型/重排模型仍按实际记录或保持未记录，避免伪造。

### 2.6 检索与 UI

- `HybridRetriever` 默认排除 `audit_hidden=1` 卡片。
- 探索中心新增“卡片状态”筛选：
  - 正常卡片
  - 建议删除的知识卡片
  - AI 审计新增
  - 审计分歧
  - 全部
- 建议删除卡片红色边框/徽标；抽屉和详情对话框可恢复。
- 文献详情新增：
  - 导出 AI 校对包
  - 导入 AI 校对结果
  - 报告包含已有审计（默认关闭）
- 审计导入对话框支持：
  - 手动填写审计 AI；
  - 从算力池节点选择审计 AI 身份；
  - 预览分数/动作/状态；
  - 逐条勾选应用；
  - 用户已确认卡片的二次确认开关。
- 卡片抽屉新增生成模型来源、AI 审计历史、多模型肯定/评价分歧徽标。

## 3. 验证

- 新增 `tests/test_v240.py`，覆盖：
  - schema v3 字段/表；
  - provenance 与内容指纹；
  - 全部卡片导出与分片；
  - 修改/软删除/新增/用户已确认保护；
  - 过期内容指纹拒绝；
  - 多 AI 分歧统计；
  - 探索中心建议删除筛选；
  - 文献详情按钮与审计导入预览。
- 真实数据库副本迁移：3,882 张卡迁移成功。
- 1,306 张卡文献导出：约 0.69s，生成 34 个分片（含 34 个提示词）。
- 100 张卡批量应用：约 0.27s。
- 全量回归：**131 passed**。

## 4. 构建与部署

- 版本：**V2.4.0**
- 发布包：
  - `release/AtomicCalculationDB_Patch_v2.4.0.zip`（438,695 B，87 个文件）
    - SHA256 `f9f39ed20ab48b08c894a578a91b36247aa62ddf051a9c92244a86be4ec0fcdf`
  - `release/AtomicCalculationDB_Full_v2.4.0.zip`（1,714,467,602 B，9,952 个文件）
    - SHA256 `9e1ecd81e56500727126d73273c876a7663db2a36718088d992e0353490bcb95`
- 两个包均通过 `unzip -t`。
- 构建时安装目录的 V2.3.10 应用仍在运行；本次未覆盖安装目录，也未部署。
