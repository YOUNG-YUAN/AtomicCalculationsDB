# FixTask_04_07.md — `AtomicCalculationDB` (V2.3.10) 删除旧版单节点模型网关，统一到算力池

## 1. 目标

删除设置页“兼容旧版单节点模型网关”整块 UI 与运行时兜底，把五个模型角色
全部纳入“统一模型与算力池”：

- 快速提取 `fast_llm`
- 深度归纳 `slow_llm`
- 图像理解 `vlm`
- 向量 `embedding`
- 重排 `reranker`

## 2. 已删除

### 2.1 Settings UI

- 删除 `兼容旧版单节点模型网关` SectionCard；
- 删除旧的五角色 Base URL / 模型 / API Key 表格；
- 删除旧的向量来源、维度、本地路径和重排来源表单；
- 删除对应 `_save_models()` 和旧配置字段引用；
- 设置页现在只保留统一算力池、内置 ONNX 引擎、下载源、存储/更新和数据维护。

### 2.2 运行时兜底

- 删除 `ModelGateway._legacy_client()`；
- 删除 `models.*` 的 LLM 客户端回退；
- `EmbeddingProvider` / `RerankerProvider` 不再读取旧
  `embedding.*` / `reranker.*` 配置；
- 未绑定角色返回明确不可用客户端，而不是静默使用旧单节点配置；
- embedding signature 从 embedding 角色节点计算（模型名 + 维度）；
- 配置默认值删除 `models`、`embedding`、`reranker` 三节。

### 2.3 迁移与 UI 行为

- 旧配置中仍有实际 Base URL / 模型值的 `fast_llm` / `slow_llm` / `vlm`
  会被一次性迁移成 `legacy-fast` / `legacy-slow` / `legacy-vlm` 节点；
- 未配置的角色不再生成空策略；
- 角色下拉新增“（未绑定）”，不会自动绑定第一个策略；
- API 节点新增“模型维度”字段，云端向量/重排节点可直接在算力池配置维度；
- 内置 ONNX 节点仍受保护，本地路径/分词器/维度可在节点编辑器中查看。

## 3. 验证

- 新增 `tests/test_v2310.py`：
  - 新配置不存在旧 `models` / `embedding` / `reranker` 节；
  - 旧 LLM 配置一次性迁移到算力池；
  - Gateway 不再读取旧 `models.fast_llm`；
  - 云端 embedding/reranker 节点驱动 provider 的参数与维度；
  - embedding signature 来自算力池节点。
- GUI 冒烟测试确认设置页没有 `model_fields` / `embed_provider` /
  `rerank_provider`，且存在 `model_pool_panel`。
- 全量回归：**118 passed**。

## 4. 构建与部署

- 版本：**V2.3.10**
- 发布包：
  - `release/AtomicCalculationDB_Patch_v2.3.10.zip`（约 398 KiB，80 个文件）
    - SHA256 `71cda9bff9379576f507fedf7000b6453bb96a8943cbf9f49dd931cf270f394d`
  - `release/AtomicCalculationDB_Full_v2.3.10.zip`（约 1,635 MB，9,945 个文件）
    - SHA256 `13ecf4b3b63a3c3659046021b0848a5158b019942f8cf1dc0ebc82c5ec144b56`
- 两个包均通过 `unzip -t`。
- 安装目录部署前需确认应用已关闭；构建时检测到已安装的 V2.3.8 仍在运行，
  因此本次未覆盖安装目录。
