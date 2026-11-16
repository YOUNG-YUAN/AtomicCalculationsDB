# FixTask_04_03.md — `AtomicCalculationDB` (V2.3.4) 全库清洗进度、ETA 与争议校验卡顿修复任务书

> **致 AI Coding Agent 的全局执行说明**：
> 本文记录 V2.3.4 对“全库本地修复与清洗”无进度反馈、无法判断剩余时间、
> 以及大重复组在 81% 处长时间停住的问题。后续修改不得重新引入“整库一次性
> 编码”或“大组全量两两 NLI 推理”。

---

## 1. 用户现象

用户在文献库点击：

```text
🧹 全库本地修复与清洗 (0 Token)
```

界面只显示一句笼统的“正在执行全库本地修复与清洗”，没有：

- 当前处于哪个阶段；
- 已处理多少文献 / 多少张卡；
- 整体百分比；
- 已用时间；
- 预计剩余时间；
- 长阶段（尤其 3,648 张卡的 MiniLM 编码）是否仍在推进。

此外，真实库测试显示聚类完成后可能长时间停在 **81%**：争议校验对每个重复组
执行 O(n²) 的 DeBERTa NLI 成对推理，大组会发起数千次推理。

---

## 2. 根因

### 2.1 后端进度被局部丢弃

- `ReprocessManager.local_clean()` 只发出 0.05 / 0.55 / 0.65 / 0.75 等粗粒度
  百分比；
- `_recluster_local()` 调用 `engine.cluster([...])` 时没有传 `progress`；
- `purge_invalid_cards()` / `sanitize_existing_cards()` 已有逐卡进度，但未接线；
- `ModelRegistry.embed_minilm()` 已按 32 条/批推理，但没有把批进度向上传递。

### 2.2 增量/强制模式进度回退

`incremental()` / `force()` 把每个文档内部的 `extract_bundles(progress=fraction)`
直接当成全库进度，导致每篇文档开始时进度条从 0 重来。

### 2.3 大重复组争议校验 O(n²)

```python
for i, left in enumerate(members):
    for right in members[i + 1:]:
        engine.detect_conflict(text(left), text(right))  # NLI inference
```

一个 100 张卡的重复组会产生 **4,950 次** NLI 推理；首次推理还要加载
DeBERTa 模型。用户看到的是 81% 长时间不动，且没有组内进度。

---

## 3. 已实施修复（V2.3.4）

### 3.1 五阶段可见进度

`app_core/reprocess.py`：

1. **阶段 1/5** 元数据与 Zotero 文件名修复（逐篇计数）；
2. **阶段 2/5** 证据截断废卡清理（逐卡计数）；
3. **阶段 3/5** 公式伪影与说明文字净化（逐卡计数）；
4. **阶段 4/5** miniLM 语义编码、分块相似度聚类（逐批计数）；
5. **阶段 5/5** NLI 争议校验与同义合并（逐对 / 逐组计数）。

整体进度权重：

```text
0.03–0.32  元数据
0.33–0.37  废卡清理
0.38–0.43  公式净化
0.44–0.81  MiniLM 编码 / 相似度
0.82–0.99  争议校验 / 合并
1.00       完成
```

### 3.2 后端进度接线

- `ReprocessManager.local_clean()` 把 `progress` 传给：
  - `repair_invalid_documents(progress=...)`
  - `purge_invalid_cards(progress=...)`
  - `sanitize_existing_cards(progress=...)`
  - `_recluster_local(progress=...)`
- `_recluster_local()` 把进度传给 `ConsensusConflictEngine.cluster(progress=...)`；
- `ModelRegistry.embed_minilm()` 每批（32 条）报告 `done/total`；
- 相似度分块每 256 行报告一次；
- 争议校验每 10 对或每组报告一次。

### 3.3 阶段 5 大组代表卡策略

`app_core/reprocess.py`：

- 组大小 ≤ 8：保持全量两两 NLI 对比；
- 组大小 > 8：选择“用户已验证 > 共识次数 > 权威分”最高的代表卡，
  只对比代表卡与其余成员（O(n)）；
- 任何一对冲突都会给整组打上冲突标记，并跳过该组的自动合并；
- 该策略把 100 张卡组的 NLI 推理从 4,950 次降到 99 次，避免 Stage 5 卡死；
- NLI 输入只使用标题 / 摘要 / 前提（回退到 search_payload），并按 16 对
  批量推理；不再把整段约 512 token 的检索载荷逐对送入 DeBERTa。

### 3.4 进度条与 ETA

新增 `app_core/ui/progress_eta.py`：

- `EtaTracker` 记录单调进度、阶段起始时间与近 30 秒速率；
- 文献库页新增整体 `QProgressBar`、阶段文字、`已用 / 预计剩余`标签；
- 单篇详情页的进度条同样显示 ETA；
- 阶段 4 排除 0.44→0.47 的初始化跳变和模型加载成本后再计算速率；
- 阶段 5 排除首次 NLI 模型加载，按已校验对数估算；
- 完成后保留“总用时”，停止后显示“已停止”，失败时隐藏进度条。

### 3.5 增量 / 强制模式进度映射

每个文档内部的提取进度映射到该文档在全库中的区间：

```python
start = 0.05 + 0.90 * (index - 1) / total
span = 0.90 / total
overall = start + span * extract_fraction
```

不会再出现每篇文档从 0 重来的进度条。

---

## 4. 验证

### 4.1 自动化测试

```bash
python -m pytest -q
```

结果：**104 passed**。

新增覆盖：

- `test_eta_tracker_formats_remaining_and_finishes`；
- `test_eta_stage_rates_ignore_model_load_and_bookkeeping`；
- `test_conflict_engine_batches_nli_pairs`；
- `test_large_duplicate_group_uses_anchor_not_all_pairs`：
  10 张同组卡片只发起 9 次 `detect_conflict`，合并后剩 1 张；
- `test_local_reprocess_cleans_formula_and_truncated_cards` 增加阶段进度
  单调性与完成事件断言；
- GUI 冒烟测试增加进度条数值与 ETA 标签断言。

### 4.2 真实库只读基准

使用安装目录 `atomic_meta.sqlite` 的 SQLite 在线备份（不改动用户库）：

```text
卡片：3,648
MiniLM：all-MiniLM-L6-v2 INT8
```

基准输出（见 `scripts/benchmark_reprocess_progress.py`）：

- 阶段 4 MiniLM 编码 3,648 张卡约 **187–200 秒**；
- 相似度分块约 **0.1 秒**；
- 聚类得到约 **1,880 个组**；
- 阶段 5 共约 **2,527 对** 冲突检查，按 32 对/批推进；在应用同时运行、
  CPU 被占用的情况下前 96 对约 30 秒，进度与计数持续更新；
- 阶段 5 不再出现“停在 81% 无任何反馈”的情况。

---

## 5. 构建与部署记录

- 版本：**V2.3.4**（build 20261102，channel v2.3）
- 发布包：
  - `release/AtomicCalculationDB_Patch_v2.3.4.zip`（382.7 KiB，SHA256 `f50d80ec139bf97aa2640f5e45b2ecc9827cc281911f076786c04cdb5ebb74f8`）
  - `release/AtomicCalculationDB_Full_v2.3.4.zip`（1,635.0 MB，9,944 个条目，SHA256 `ae13b4fe1654bcb2895230b8e8a244c5a1faa7aed58d8ef39cd7950dafd4b068`）
  - 两个压缩包均通过 `unzip -t` 完整性检查；
  - 完整包包含 BGE-M3、MiniLM、NLI 与语义精排权重。
- 安装目录：`C:\Normal Program Files\AtomicCalculationDB`
- **部署状态**：构建完成时应用仍在运行（用户未关闭），按运行期保护规则
  **未覆盖安装目录**；更新包已就绪，可在设置页导入或等待应用退出后部署。
- 部署前必须确认应用已退出，禁止覆盖正在运行的安装目录。

---

## 6. 验收标准

- [x] 全库本地清洗显示五阶段文字与整体百分比；
- [x] 显示已用时间与预计剩余时间；
- [x] 每 32 张卡的 MiniLM 编码都会推进进度；
- [x] 阶段 5 按已校验对数推进；
- [x] 大重复组不再执行 O(n²) NLI；
- [x] 增量/强制模式进度不再逐文档归零；
- [x] 104 项测试通过；
- [x] 真实库 3,648 张卡只读基准验证编码 / 对标进度（完整清洗端到端将在部署后由用户运行）；
- [x] V2.3.4 更新包 / 完整包生成并校验；
- [ ] 部署到安装目录（等待应用退出）。
