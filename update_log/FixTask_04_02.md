# FixTask_04_02.md — `AtomicCalculationDB` (V2.3.3) 全库本地清洗内存爆炸与 ONNX 分批安全修复任务书

> **致 AI Coding Agent 的全局执行说明**：
> 本文记录 V2.3.3 的致命内存问题、已实施的修复、真实数据验证结果，以及后续
> “全库向量重建/回填”必须遵守的内存安全约束。任何后续优化不得把已分批的
> MiniLM/BGE 推理重新改回“整库一次性 encode_batch”。

---

## 1. 事故现象

用户准备做全库向量重建，随后点击：

```text
文献库 → 🧹 全库本地修复与清洗 (0 Token)
```

结果：

- Python/运行时内存占用瞬间升至 **99%～100%**；
- 电脑严重卡顿直至几乎不可操作；
- 用户数据库约 **3,648 张 L2 知识卡**；
- 已确认不是单个 PDF 或数据库损坏，而是本地清洗中的全库嵌入/聚类阶段。

---

## 2. 根因分析

### 2.1 MiniLM 全库一次性编码（主因）

`ReprocessManager.local_clean()` 会调用 `_recluster_local()`：

```text
local_clean()
  → _recluster_local()
    → ConsensusConflictEngine.cluster(texts)
      → ModelRegistry.embed_minilm(texts)
```

旧实现把 3,648 张卡的 `search_payload` **一次性**送入：

```python
encodings = tokenizer.encode_batch(list(texts))
input_ids = np.array([...])       # [3648, seq_len]
attention = np.array([...])
outputs = session.run(...)        # token_embeddings
hidden = np.asarray(outputs[0])   # [3648, seq_len, 384]
```

当序列长度约 512 时，仅 `token_embeddings` 就接近：

```text
3648 * 512 * 384 * 4 bytes ≈ 2.8 GB
```

再叠加 ONNX Runtime 的 CPU memory arena、中间张量和 Python 对象，足以把
8 GB/16 GB 机器推到 99%～100%。

### 2.2 BGE-M3 与 MiniLM 同时驻留

如果用户刚做过全库向量重建，`EmbeddingProvider` 持有 BGE-M3 ONNX 会话，
模型权重和外部分片数据约 **1.4～2.3 GB**。随后全库清洗又加载 MiniLM，
两套本地模型同时驻留，进一步放大峰值。

### 2.3 聚类相似度使用 Python 双重循环

旧 `ConsensusConflictEngine.cluster()` 对 3,648 张卡执行：

```python
for i in range(count):
    for j in range(i + 1, count):
        ...
```

约 **660 万对** Python 循环，速度慢且容易让用户误以为程序卡死；虽然主要内存
峰值来自 2.1，但该循环必须一并改为分块向量化。

---

## 3. 已实施修复（V2.3.3）

### 3.1 分批 MiniLM 嵌入

文件：`app_core/builtin_models/model_registry.py`

- `embed_minilm(texts, *, batch_size=32)`；
- 默认每批 **32** 条，最大允许 256；
- 每批独立执行 tokenizer 编码、ONNX 推理、池化、归一化；
- 结果按原顺序拼接，不改变语义和向量维度。

### 3.2 分批 BGE-M3 本地嵌入

文件：`app_core/models/gateway.py`

- `EmbeddingProvider._embed_local(texts, *, batch_size=16)`；
- 默认每批 **16** 条，最大 64；
- 使用模型提供的 `sentence_embedding` 输出（BGE-M3 为 1024 维），
  不再对 `token_embeddings` 做全库大张量平均池化；
- 保持 `BAAI/bge-m3::dim=1024` 的 embedding signature 不变。

### 3.3 限制 ONNX Runtime 内存

- `ModelRegistry.onnx_session()`：关闭 `enable_cpu_mem_arena` 和
  `enable_mem_pattern`，最多 4 个 intra-op 线程；
- `EmbeddingProvider` 加载 BGE-M3 时使用同样的 SessionOptions；
- 目的：避免 arena 随批次反复增长，把已释放张量长期留在进程工作集。

### 3.4 清洗前释放 BGE-M3 会话

文件：`app_core/reprocess.py`

在 `local_clean()` 进入 `_recluster_local()` 之前调用：

```python
gateway.embedding.release_local_session()
```

`EmbeddingProvider.release_local_session()` 会释放 BGE-M3 的 ONNX session 和
tokenizer 并执行 `gc.collect()`。实测 BGE-M3 会话释放后，进程 RSS 可从约
1.5 GB 回落到约 100 MB。

### 3.5 分块向量化聚类

文件：`app_core/parser/consensus_conflict_engine.py`

- 若 MiniLM 向量可用，先归一化；
- 按 **256 行**分块计算 `block @ matrix.T`；
- 使用 `np.argwhere` 获取超过阈值的索引并执行并查集 union；
- 不再对 660 万对执行 Python 双层循环；
- 若模型不可用，仍回退到原有 token 启发式，但不会加载大模型。

---

## 4. 真实数据验证

使用用户实际数据库的只读快照：

```text
L2 知识卡：3,648
MiniLM：all-MiniLM-L6-v2 ONNX
```

验证结果：

| 指标 | 旧实现 | V2.3.3 |
|---|---:|---:|
| 聚类峰值 RSS | 约 2 GB（未分批时可达数 GB，直接 OOM） | **约 150 MB** |
| 聚类耗时 | 不稳定/易卡死 | **约 146 秒** |
| BGE-M3 释放后 RSS | 保持约 1.5 GB | **约 111 MB** |
| 全量测试 | — | **100 passed** |

说明：146 秒是 CPU + ONNX Runtime 关闭 memory arena 后的单次全库聚类基准；
不应再出现内存打满，但用户应能看到“本地 ONNX 聚类与争议校验”进度。

---

## 5. 验证测试

新增/更新：

- `tests/test_v23.py`：
  - 合成分块向量聚类测试：200 条文本 → 4 个聚类组，验证向量化路径；
  - 既有 builtin local node、endpoint pool、queue/relay、clipboard 测试保持通过。

要求任何后续修改必须继续通过：

```bash
python -m pytest -q
```

并额外进行真实数据只读基准：

- 3,648 卡全库聚类峰值 RSS < 500 MB；
- 不允许再次出现一次性 `encode_batch` 全库输入；
- 不允许 BGE-M3 与 MiniLM 同时常驻进行全库清洗。

---

## 6. 构建与部署记录

- 版本：**V2.3.3**（build 20261102，channel v2.3）
- 发布包：
  - `release/AtomicCalculationDB_Patch_v2.3.3.zip`（378 KB，SHA256 5b0f3f91…）
  - `release/AtomicCalculationDB_Full_v2.3.3.zip`（1.6 GB，SHA256 f0fcf972…）
- 安装目录：`C:\Normal Program Files\AtomicCalculationDB`
- 安装版 GUI 启动/退出冒烟通过；
- 部署后保持关闭，等待用户手动打开。

---

## 7. 剩余工作：全库向量回填（后续任务）

当前数据库仍有：

```text
knowledge_cards: 3,648
is_vectorized: 0
```

即现有卡片尚未写入 Qdrant 向量。V2.3.3 只修复了内存安全问题，没有自动执行
全库向量重建。后续实现必须遵守：

1. 使用 BGE-M3 分批嵌入（默认 16 条/批，绝不整库一次性编码）；
2. 每批完成即 upsert 到 Qdrant 并更新 `is_vectorized=1`；
3. 支持断点续跑：跳过已向量化卡片；
4. 提供进度回调：`已处理 N / 总数 M`、当前批次耗时、内存状态；
5. 全库回填前释放 MiniLM/NLI 会话；回填后再按需加载；
6. 失败时保留已完成批次，不把数据库或 Qdrant 留成半写状态；
7. 提供取消按钮，取消后允许从断点继续；
8. 若用户选择“全库本地修复与清洗”，不得在 MiniLM 聚类期间同时持有
   BGE-M3 会话。

---

## 8. 验收标准

- [x] 3,648 卡全库本地清洗峰值 RSS 约 150 MB，不再打满内存；
- [x] BGE-M3 会话可在聚类前释放；
- [x] MiniLM/BGE 均按批推理；
- [x] 聚类相似度使用分块向量化；
- [x] 100 项测试通过；
- [x] V2.3.3 安装包与完整包已生成；
- [x] 安装版 GUI 冒烟通过；
- [ ] 全库向量回填（3,648 张卡）尚未执行，作为独立任务实现。
