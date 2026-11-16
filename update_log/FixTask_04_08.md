# FixTask_04_08.md — `AtomicCalculationDB` (V2.3.11) 探索中心分页

## 1. 问题

探索中心通过 `SearchWorker` 固定请求 `limit=30`，因此文献详情显示上千张关联
知识卡时，探索中心最多只显示 30 张，且没有翻页入口。数据库中的卡片并未丢失。

## 2. 实现

### 2.1 检索层

- `HybridRetriever.search_page(query, page, page_size, extra_filters)`：
  - 空查询：直接使用 SQLite `list_knowledge_cards(limit, offset)` 和
    `count_knowledge_cards()`，全部卡片都可分页浏览，总条数精确。
  - 有查询：首次构建完整匹配列表并按现有 RRF / 重排 / 权威度融合排序，
    结果按 query + filters 缓存；翻页复用同一排名，避免每页重新洗牌。
  - 超过 200 个候选时跳过本地 cross-encoder 重排，改用确定性词法分数，
    避免对上千张卡片执行昂贵的 ONNX 成对推理；小结果集保持原有重排。
  - 精确 ID 查询仍返回单条结果。
- `SQLiteManager.search_cards_fts()` 的 FTS/LIKE 召回上限从 200 提升到 5000，
  支持探索中心构造完整结果集。
- `SearchWorker` 新增 `page` / `page_size` 和 `finished_page(hits, total)` 信号。

### 2.2 探索中心 UI

- “显示 10 / 20 / 30 / 50 / 100”每页条数选择，选择后写入
  `explore.page_size` 并持久化。
- 首页、上一页、下一页；页码按钮共最多 9 个（当前页 4 个前页 + 当前页 +
  4 个后页），靠近首页/末页时自动贴边。
- 页码输入框 + “跳转”，支持回车。
- 显示“当前区间 / 总条数”和“第 X / Y 页”。
- 布局顺序调整为：受控关键词 → 分页控件 → 结果标题 → L1/L2 卡片；
  关键词位于页码按钮上方，分页控件位于卡片上方。
- 翻页后自动滚回探索中心顶部。
- 保留最多两个 L1 总纲横幅；同页其余 L1 卡片按普通卡片显示，不静默丢弃。
- 新增 `PageButton` / `PageActive` 样式。

## 3. 验证

- 新增 `tests/test_v2311.py`：
  - 空查询分页总条数精确、页与页之间无重复；
  - 查询分页稳定、缓存生效、第三页数量正确；
  - 九页窗口在首页、中间页、末页的贴边行为；
  - GUI 翻页：25 张卡片、每页 10 张，下一页和页码跳转均正确。
- 全量回归：**122 passed**。
- 真实数据库副本基准：
  - 空查询：3,882 张卡片，页 1 返回 20 张，约 0.01s；
  - `MCDHF`：978 条匹配，约 0.12s；
  - `active space`：567 条匹配，约 0.06s。

## 4. 构建与部署

- 版本：**V2.3.11**
- 发布包：
  - `release/AtomicCalculationDB_Patch_v2.3.11.zip`（410,591 B，80 个文件）
    - SHA256 `435bb590c597b4040f84c9d2f41d790775c95c7af4e09afbdf37ba1f3356cc4b`
  - `release/AtomicCalculationDB_Full_v2.3.11.zip`（1,714,438,428 B，9,945 个文件）
    - SHA256 `245c7e47d4b0f11cb7b3a66e7d4699b60a1205a87c3de7cdad054d9aa80e3799`
- 两个包均通过 `unzip -t`。
- 构建时安装了 V2.3.10 的应用仍在运行；本次未覆盖安装目录，也未部署。
