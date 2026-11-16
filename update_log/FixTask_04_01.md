# FixTask_04_01.md — `AtomicCalculationDB` (V2.3.1 → V2.3.2) 本地 ONNX 模型内置算力池、受保护节点与“空链”修复任务书

> **致 AI Coding Agent 的全局执行说明**：
> V2.3.0 已实现模型算力池、策略编排、分时段角色路由、动态分包和接力执行器。
> V2.3.1 已把算力池节点实际调用的模型名称直接显示在列表中。
> 本次 V2.3.2 要解决用户看到的“向量模型策略 / 重排模型策略显示（空链）”问题，
> 并把程序自带的本地 ONNX 模型正式纳入统一算力池，作为不可删除的内置节点。
> 请严格按以下任务完成实现、迁移、测试、截图和重新打包。

---

## 0. 背景：为什么会出现“空链”？

当前 `EndpointPool._seed_from_legacy()` 在旧配置没有显式算力池节点时，会为
`fast_llm / slow_llm / vlm` 创建真实 API 节点，但只为 `embedding / reranker`
创建了**没有链成员的策略占位对象**：

```text
向量模型策略 · （空链）
重排模型策略 · （空链）
```

因此“空链”不是网络错误，也不是策略损坏，而是：

1. 当前配置里没有把本地 BGE-M3 / 语义精排模型注册成算力池节点；
2. 旧版向量和重排模型仍由 `EmbeddingProvider` / `RerankerProvider` 直接读取
   `workspace/models`，没有经过算力池；
3. 设置页仍然为这两个角色显示了占位策略，但 `relay_chain == []`。

V2.3.2 必须消除这种“策略存在但没有节点”的默认状态。

---

## 1. V2.3.1 已完成内容（本次任务书的前置状态）

以下内容已经实现，不需要重做，但需要在最终验收中保留：

- `app_core/ui/model_pool_panel.py` 的算力池列表直接显示节点模型名：
  `快速提取 API · OpenAI 兼容 API · 🧠 deepseek-flash · ...`
- 节点列表鼠标悬停显示节点 ID、模型和 Base URL。
- V2.3.1 版本号、package 补丁和完整包已生成；`pytest` 97 项通过。
- 本任务书的 V2.3.2 是在此基础上继续，不要把上述显示逻辑改回只显示名称。

---

## 2. Task 1：定义并自动注入“程序自带本地模型节点”

### 2.1 扩展节点模型 (`app_core/models/endpoint_pool.py`)

为 `EndpointNode` 增加以下字段（保持旧配置可迁移）：

```text
builtin: bool = False          # 程序内置节点
deletable: bool = False        # 是否允许用户删除
role_hint: str = ""            # embedding / reranker / ...
model_path: str = ""           # 本地 ONNX 文件或模型目录
tokenizer_path: str = ""       # 本地 tokenizer 文件
model_dim: int = 0             # 向量维度（BGE-M3 为 1024）
integrity_sha256: str = ""     # 可选，内置模型文件校验值
```

要求：

1. `EndpointNode.as_dict()` / `from_dict()` 完整保留新字段；
2. 旧配置缺少这些字段时使用默认值，不破坏现有节点；
3. `EndpointPool.remove_node(node_id)` 对内建节点必须拒绝删除（返回 `False`
   或抛出明确的 `ProtectedEndpointError`），并在日志/UI 中说明原因；
4. 内置节点的 `node_id` 固定，不能通过编辑改名后“绕过”删除保护。

### 2.2 内置节点与默认策略

在 `EndpointPool` 启动加载时，必须**幂等**创建以下节点（已存在则补齐字段，
不覆盖用户对非保护字段的修改）：

| 固定 node_id | 显示名称 | node_type | capabilities | 模型 / 文件 | 默认策略 |
|---|---|---|---|---|---|
| `builtin-local-bge-m3` | 本地 BGE-M3 向量模型 | `LOCAL_ONNX` | `["EMBEDDING"]` | `BAAI/bge-m3`；`workspace/models/bge-m3-int8.onnx` + tokenizer | `builtin-local-embedding-strategy` |
| `builtin-local-reranker` | 本地语义精排模型 | `LOCAL_ONNX` | `["RERANK"]` | `BAAI/bge-reranker-v2-m3` 兼容本地 MiniLM reranker；`ms-marco-MiniLM-L-6-v2-int8.onnx` + tokenizer | `builtin-local-reranker-strategy` |
| `builtin-local-minilm` | 本地 MiniLM 语义引擎 | `LOCAL_ONNX` | `["EMBEDDING"]`（内部语义聚类/排序辅助，不强制作为 BGE-M3 替代） | `all-MiniLM-L6-v2-int8.onnx` + tokenizer | 不改变默认 BGE-M3 绑定，供高级策略选择 |
| `builtin-local-nli` | 本地 DeBERTa NLI 冲突引擎 | `LOCAL_ONNX` | `["RERANK"]` 可标记为辅助能力，实际用于冲突探测 | `nli-deberta-v3-xsmall-int8.onnx` + tokenizer | 默认不替换语义精排策略，供高级策略选择 |

> 说明：BGE-M3 是 1024 维向量空间；MiniLM 是 384 维，**不能**在未迁移 Qdrant
> 向量库的情况下冒充 BGE-M3。`builtin-local-minilm` 和 `builtin-local-nli`
> 只作为本地智能引擎节点展示/高级编排，不得破坏现有 embedding signature。

内置节点必须带：

```text
builtin = True
deletable = False
```

并且在算力池列表中显示 `🔒 内置`，在节点编辑弹窗中显示只读提示。

### 2.3 默认角色绑定

当角色绑定为空、或旧版 `EndpointPool._seed_from_legacy()` 生成了空链时：

```text
embedding.default_strategy_id  -> builtin-local-embedding-strategy
reranker.default_strategy_id   -> builtin-local-reranker-strategy
```

对应策略的 `relay_chain` 必须至少包含一个本地内置节点：

```text
builtin-local-embedding-strategy -> ["builtin-local-bge-m3"]
builtin-local-reranker-strategy  -> ["builtin-local-reranker"]
```

用户已有的自定义策略和绑定必须保留，不得被默认值覆盖。只有在用户的绑定
指向空链/失效策略时，才自动修复到内置默认策略。

### 2.4 模型文件由程序自带

1. 完整包 `AtomicCalculationDB_Full_*.zip` 必须包含：
   * `workspace/models/bge-m3-int8.onnx` + `bge-m3-tokenizer.json`（以及对应的
     SHA256 校验文件，若存在）；
   * 现有 `all-MiniLM-L6-v2`、`nli-deberta-v3-xsmall`、`ms-marco-MiniLM-L-6-v2`
     ONNX 与 tokenizer 文件；
   * `en_core_web_sm` 继续随 runtime 提供。
2. 安装后不要求用户联网下载内置模型；设置页显示“内置模型已就绪”。
3. 内置节点即使模型文件缺失也不能删除，只能显示：
   `⚠️ 内置模型文件缺失：请修复安装包/重新安装`；
   且向量检索回退到 FTS，不得崩溃。
4. 如果 BGE-M3 文件过大导致完整包体积变化，必须在 README 和打包日志中明确
   记录体积；不得静默从完整包中排除。

---

## 3. Task 2：让本地模型真正进入网关，而不只是“显示在池里”

### 3.1 Embedding 路由 (`app_core/models/gateway.py`)

1. `EndpointPool.node_for_role("embedding")` 默认应返回
   `builtin-local-bge-m3`（内置节点）。
2. 当该节点启用且文件齐全时，`ModelGateway.embedding` 必须使用该节点的
   `model_path/tokenizer_path/model_dim`，保持 `BAAI/bge-m3::dim=1024` 的现有
   `embedding_signature`，不得改变 Qdrant collection 维度。
3. 若用户把 `embedding` 角色绑定到自定义云端/其他策略，仍按现有配置执行；
   内置节点作为默认值和回退，不强抢用户显式选择。
4. 文件缺失时：
   * 不删除节点；
   * `EmbeddingProvider.available == False`；
   * 检索回退 FTS；
   * 在设置页和日志显示具体缺失文件路径。

### 3.2 Reranker 路由

1. `EndpointPool.node_for_role("reranker")` 默认返回
   `builtin-local-reranker`；
2. `ModelGateway.reranker` 使用 `ms-marco-MiniLM-L-6-v2`（或后续兼容的本地
   reranker）文件，保持现有 `RerankerProvider` 降级行为；
3. 文件缺失时同样只能告警，不能删除内置节点，也不能阻止应用启动。

### 3.3 不允许的行为

- 不允许在 `EndpointPool.load()` 中因为“旧配置没有节点”就删除或跳过内置节点；
- 不允许把 BGE-M3 的 1024 维向量空间静默改成 MiniLM 的 384 维；
- 不允许内置节点被 `remove_node`、策略编辑弹窗删除，或通过修改 `node_id` 绕过；
- 不允许完整包依赖用户手动下载 BGE-M3 才能达到默认向量检索状态（若确实缺失，
  必须在验收中作为明确缺陷记录）。

---

## 4. Task 3：设置页 UI 修复

### 4.1 算力池列表

每个节点行显示：

```text
本地 BGE-M3 向量模型 · 本地 ONNX · 🧠 BAAI/bge-m3 · 🔒 内置
  · 📦 32k Tokens / 20片段 · 🕒 全天 · ⏱️ 20s 轮询 / 120s 转交
```

要求：

- 继续保留 V2.3.1 的模型名显示；
- 显示 `🔒 内置`；
- 本地节点悬停提示显示 `model_path`、`tokenizer_path` 和完整性状态；
- 本地模型行使用区别于云端 API 的视觉徽章（但不要破坏 Material 3 主题）。

### 4.2 节点编辑 / 删除

- 内置节点：`编辑` 按钮可用但模型类型/能力/文件路径/固定 `node_id` 只读；
  可调整显示名称、可用时段、轮询参数（是否允许调整由实现决定，但必须保证
  内置节点不会因改名而被绕过保护）。
- `删除` 按钮对内置节点禁用，并显示提示：
  `程序自带模型节点不可删除；如需停用，请在角色绑定中切换策略。`
- 策略编排器：
  * 内置节点不能从**内置默认策略**中移除；
  * 用户自定义策略可以引用/不引用内置节点，但不能删除节点本身；
  * 默认策略列表中不得再出现“向量模型策略 · （空链）”或
    “重排模型策略 · （空链）”。

### 4.3 空链修复后的预期

设置页应显示类似：

```text
向量模型策略 · 本地 BGE-M3 向量模型 · 不循环
重排模型策略 · 本地语义精排模型 · 不循环
```

角色路由预览应显示：

```text
向量模型: 策略「向量模型策略」→ 本地 BGE-M3 向量模型
重排模型: 策略「重排模型策略」→ 本地语义精排模型
```

对于旧配置中已经存在的空链，加载时应自动修复；无法自动修复时给出明确提示，
而不是继续显示空链。

---

## 5. Task 4：迁移、兼容与保护测试

新增测试至少覆盖：

1. **旧配置迁移**
   * 空 `endpoint_pool` + 旧 `models.fast_llm/slow_llm/vlm/embedding/reranker`；
   * 加载后存在 `builtin-local-bge-m3`、`builtin-local-reranker`；
   * `embedding`/`reranker` 默认策略链非空；
   * 再次 `load()` 不会重复创建节点或清空用户自定义策略。
2. **不可删除保护**
   * `pool.remove_node("builtin-local-bge-m3")` 被拒绝；
   * UI 删除按钮对内置节点禁用；
   * 修改内置节点 `node_id` 不得绕过保护（如禁止该字段编辑）。
3. **文件缺失降级**
   * 临时缺少 BGE-M3 文件时节点仍存在、`available=False`、检索回退 FTS、
     应用可启动；
   * 不抛出未捕获异常，不删除内置节点。
4. **维度保护**
   * 默认 embedding signature 仍为 `BAAI/bge-m3::dim=1024`；
   * 不允许 MiniLM 384 维模型改写 BGE-M3 绑定。
5. **设置页**
   * 策略列表不含“空链”；
   * 节点行包含模型名和 🔒 内置；
   * 删除按钮状态正确。
6. **打包**
   * 完整包中存在 BGE-M3 / MiniLM / NLI / reranker / tokenizer 文件；
   * 完整包启动后无需联网即可显示内置模型节点；
   * 97+ 项测试通过。

---

## 6. Task 5：版本、截图与重新打包

1. 将版本升级为 **V2.3.2**（`build` 使用实际构建日期）。
2. 更新：
   * `app_core/version.json`
   * `app_core/main_entry.py`
   * `app_core/ui/main_window.py`
   * `app_core/ui/widgets.py`
   * `native_launcher/launcher.rc`
   * `README.md`
3. 更新截图：
   * `docs/settings.png` / `docs/settings_v23.png`
   * `docs/settings_pool_v23.png`（必须能看到本地 BGE-M3 与语义精排节点及模型名）
4. 重新生成：
   * `release/AtomicCalculationDB_Patch_v2.3.2.zip`
   * `release/AtomicCalculationDB_Full_v2.3.2.zip`
5. 更新安装目录 `C:\Normal Program Files\AtomicCalculationDB`，执行：
   * `--mcp` 启动测试；
   * GUI 启动/退出冒烟；
   * 确认无 Qdrant 锁冲突（不要在已有实例运行时启动第二个实例）。

---

## 7. 验收标准（Definition of Done）

- 设置页“向量模型策略”和“重排模型策略”**不再显示空链**；
- 算力池默认显示本地 BGE-M3、语义精排、MiniLM、NLI 等内置节点，并显示
  实际模型名；
- 本地模型节点带 `🔒 内置`，删除按钮不可用，无法通过编辑绕过；
- BGE-M3 默认参与 1024 维向量检索，语义精排默认参与精排；缺失时安全降级；
- 完整包自带所需本地模型文件，不要求用户另行下载；
- 旧配置可平滑迁移，用户自定义节点/策略保留；
- 全量测试通过，安装版 V2.3.2 启动和 GUI 冒烟通过；
- 若某项无法满足（尤其是 BGE-M3 体积或授权限制），必须在最终交付说明中
  明确列出，不得只显示“已修复”。
