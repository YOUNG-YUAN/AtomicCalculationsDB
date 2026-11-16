# FixTask_07_09.md — 网页 AI 半自动批量提问包

## 1. 问题

原界面中“强制重新提炼”直接启动 `ReprocessWorker` 调用 API；单独的任务包按钮只
生成**一个** `ClipboardPacket`，关闭窗口后状态丢失，也没有多文献批量、Token
预算拆分、逐包导入和统一写库。

用户需要类似 `AIPowerpointGenerator` 的流程：

```text
选择文献
  → 设定单次提问 Token 上限
  → 一次性生成多个提问包
  → 逐个复制提示词给网页 AI
  → 导入每包回复
  → 全部就绪后统一写入知识库
```

## 2. 新增后端

`app_core/models/semi_auto.py`：

- `create_run()`：收集文献中尚未提炼（或 force 模式下的全部）证据包；
- 使用 `DynamicBatchPacker` + `ClipboardBatchService.build_packets()` 按预算拆包；
- 持久化到 `workspace/semi_auto/<run_id>/`；
- `import_reply()`：只解析并保存回复，不写数据库；
- `finalize_run()`：验证全部回复后，force 模式先删除未人工确认旧卡，再统一
  调用 `pipeline._store_draft()` 写库；
- `find_unfinished_run()` / `list_runs()`：关闭再打开可恢复任务；
- `delete_batch()` / `delete_run()`：删除单个或整批未写入任务包。

运行目录结构：

```text
workspace/semi_auto/semi_YYYYMMDD_HHMMSS_ffffff/
  manifest.json
  batch_01/
    prompt.txt
    items.json
    reply.txt
  batch_02/
    ...
```

`items.json` 保存每个 ITEM 对应的 `doc_id` 和完整 `ContextBundle`，因此导入回复
时不依赖当前 `bundles.json` 的行号。

## 3. Token 拆分

- 用户设定 `max_batch_tokens`，默认 **80,000**，保存到
  `semi_auto.max_batch_tokens`；
- 系统提示、全局文献清单、每个 ITEM 的文本都计入预算；
- 单包超过预算时由 `DynamicBatchPacker` 自动拆分；
- 每个 ITEM 前插入：

```text
【文献】DOC-PAP-... · 文献标题
```

跨文献包不会混淆归属。

## 4. 两阶段写库

这是与旧任务包逻辑最重要的区别：

1. **生成阶段**：只生成提示词，不改数据库；
2. **导入阶段**：解析回复并写入 `reply.txt`，仍不改数据库；
3. **写入阶段**：所有包状态为 `IMPORTED` 后才允许点击“写入知识库”。

force 模式在写入阶段才删除未人工确认旧卡，并保留 `is_user_verified=1` 卡片。
因此用户在生成或导入过程中取消，不会提前破坏现有卡片。

## 5. UI

新增 `app_core/ui/semi_auto_dialog.py`：

- 文献列表摘要；
- Token 上限输入；
- **“🧠 解析 / OCR 当前文献”**：先解析/OCR，生成 `bundles.json`；
- “第一步：生成提问包”；
- 每包状态、Token 估算、复制提示词、导入回复文件、粘贴回复文本、删除；
- “第二步：写入知识库”；
- 打开任务包目录、清空任务包；
- 关闭后重新打开自动恢复未完成任务。

入口：

- 文献详情“🔥 强制重新提炼（网页半自动）”；
- 文献库“🔥 强制重提选中文献 (网页半自动)”（支持多选跨文献）；
- 工作台“📚 批量任务台”；
- 原 API 强制重提保留为独立的“⚡ API 强制重提”按钮。

## 6. 测试

新增 `tests/test_semi_auto.py`：

- 小预算强制拆成多个包；
- 提示词包含文献标题/ID；
- 导入回复后数据库仍无新卡；
- 所有包导入后 finalize 才写卡；
- force 模式删除旧未确认卡、保留人工确认卡；
- 删除任务包、恢复未完成任务；
- 任务台对话框恢复已持久化任务。

全量回归：**163 passed**。

## 7. 边界

- 提问包是纯文本证据包，不附带 PDF/图片；这与本项目“不做图像理解”的约束一致。
- 网页 AI 仍然需要人工复制/粘贴；不做浏览器自动提交。
- 已完成（FINALIZED）的任务包不会自动删除，可由用户清空。
- 写入阶段不是单个 SQLite 事务；若进程在写库中途崩溃，可能留下部分新卡，需要
  重新执行 force 写入或后续补充事务化。

## 8. 发布

已升级到 **2.5.3** 并生成：

- `release/AtomicCalculationDB_Patch_v2.5.3.zip`（463.6 KiB，96 个文件，SHA256 `097544f8e7f7a10629b61c2534e938b7d83f12756fb850c816db88fa278c84fc`）
- `release/AtomicCalculationDB_Full_v2.5.3.zip`（1,635.1 MB，9,961 个文件，SHA256 `87dcfab3b8a7e48a7ecedb600824c03042ed36374e1e8f126b22aa6d98f49d6d`）

两个包均通过 `unzip -t`。构建时安装目录中的 2.5.2 仍在运行，因此按约定只打包，未强制关闭和替换；关闭后可通过补丁包更新。


## 9. 2.5.4 补充

半自动任务台增加“🧠 解析 / OCR 当前文献”按钮：

- 调用 `pipeline.parse_document()`，不触发 API 提取；
- MinerU 的 `ocr_mode=auto/ocr` 会自动处理无文本层 PDF；
- 成功后显示文献数和证据包数，再允许生成提问包；
- 解析失败的文献会列出，并提示检查 MinerU/OCR；
- 解析过程在 `ParseWorker` 后台线程运行，不阻塞界面。

## 10. 2.5.4 发布

- `release/AtomicCalculationDB_Patch_v2.5.4.zip`（465.1 KiB，96 个文件，SHA256 `eb8023381e51bd6299e79c239754b84a8f51bbebd4f70437b54f8cd5d550eb75`）
- `release/AtomicCalculationDB_Full_v2.5.4.zip`（1,635.1 MB，9,961 个文件，SHA256 `dc19e7bea644ad974e255fdafff8f99b45ab8d13830013d09a30af542a5fe6e7`）

两个包均通过 `unzip -t`，包含“解析 / OCR 当前文献”按钮和 `ParseWorker`。

## 11. 2.5.5 回复兼容修复

实机问题：网页 AI 对 30 个 ITEM 返回了 22 个 `skip: true`，其余 8 个把卡片字段
直接放在 `result` 下，没有放进 `result.cards` 数组。原导入器只验证
`ExtractionResult`，因此看到“30 个 item 都收到”，但每个 result 的
`cards` 都是空数组，最终“写入 0 张卡”。

修复：

- `KnowledgeCardDraft` 接受常见 `card_type` 别名：
  `CONCLUSION → CONC`、`FORMULA/EQUATION → CODE` 等；
- `z_num="unknown"` 自动转 `None`；
- `formulas_latex` 数组自动换行拼接为字符串；
- 根级 `unknown_fields` 合并到 `conditions.unknown_fields`；
- `ClipboardBatchService.parse_reply()` 识别单卡片直接位于 `result` 下的格式；
- 非 `skip` 且没有任何 cards 的 item 不再被当作有效结果；
- 导入提示显示“解析到 N 张卡”；
- 写入阶段若仍为 0 张，明确提示 AI skip/空 cards 的数量；
- 生成提问包时附加完整 `ExtractionResult` JSON Schema 和明确的
  `result.cards` 输出格式，降低后续格式漂移。

测试：**165 passed**。

## 12. 2.5.5 发布

- `release/AtomicCalculationDB_Patch_v2.5.5.zip`（466.3 KiB，96 个文件，SHA256 `adea9329a70e67d659e6ba0d8c9b5eae3cdce9158b77617172ae6aef874cce63`）
- `release/AtomicCalculationDB_Full_v2.5.5.zip`（1,635.1 MB，9,961 个文件，SHA256 `6a23636ea04dffe8a79c0b081ea0902f53fe3390fe287ae3cacacd00dbf2985b`）

两个包均通过 `unzip -t`，包含单卡片 `result` 兼容、`CONCLUSION/FORMULA` 别名归一、卡片计数提示和 0 卡警告。

## 13. 2.5.6 发布

新增 `METHOD/METHODOLOGY/APPROACH → STRAT` 归一；未识别的 card_type 按关键词回退到合法类型，避免整项被判为缺失。实测用户新回复：

- prompt：30 个 ITEM（`DOC-PAP-0000-43C124-0..29`）
- answer：30 个 item_id，21 个 skip + 9 个单卡片
- 2.5.5 解析 25 个（5 个 `METHOD` 卡校验失败，报“缺少 5 项”）
- 2.5.6 解析 30 个，恢复 9 张卡（5 STRAT + 4 CODE）

包：

- `release/AtomicCalculationDB_Patch_v2.5.6.zip`（466.6 KiB，96 个文件，SHA256 `c5473e7a09c49aaa47fb08f1303fde7b158405e142cf6c1b55894497d0cfe0d0`）
- `release/AtomicCalculationDB_Full_v2.5.6.zip`（1,635.1 MB，9,961 个文件，SHA256 `980ea7755a58b7625b0d0c682985320ea4f759ad85f2f3cc069aa0227ab3c1e5`）

两个包均通过 `unzip -t`。安装目录仍为 2.5.5 且程序运行中，尚未替换。
