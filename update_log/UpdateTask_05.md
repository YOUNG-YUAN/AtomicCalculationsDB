# UpdateTask_05.md — `AtomicCalculationDB` (V2.4.0) 知识卡跨模型审计、文献总结报告导出与审计结果回写

> **执行目标**：让用户把“文献 PDF + 该文献全部知识卡总结报告 + 审计提示词”交给另一个更强的 AI 校对；再把外部 AI 返回的 JSON 导入程序，将修改、建议删除、新增卡片写回数据库。导入和回写过程**不调用任何大模型、不消耗 Token**，只做本地解析、校验、数据库事务和确定性的共识/冲突重算。

---

## 1. 背景与问题

当前一篇文献可以生成数百到上千张知识卡。例如现有数据库中：

- `DOC-PAP-0000-077BFD`：1,306 张关联知识卡
- `DOC-PAP-2018-1B71B2`：1,214 张
- `DOC-PAP-0000-493E5A`：475 张

卡片数量多、来源模型多，单次提取出现误读、前提遗漏、模型代号未消解、公式/代码错误是正常的。现有系统只有“生成”和“人工编辑”，缺少：

1. 导出“整篇文献的全部知识卡”供外部 AI 审阅；
2. 记录每张卡由哪个 AI 生成、由哪个 AI 校对；
3. 导入外部 AI 的修改/删除/新增建议；
4. 多模型评价的共识与分歧可视化；
5. 对“建议删除”的卡片做软删除、红色标记和可恢复入口；
6. 审计后自动更新学术争议与策略冲突。

本任务新增一条完整的 **“导出 → 外部 AI 审计 → 导入 → 人工复核”** 闭环。

---

## 2. 核心原则与安全边界

### 2.1 不做破坏性操作

- 外部 AI 建议删除卡片时，**绝不执行物理 `DELETE`**。
- 删除只转换为软状态：`audit_status = SUGGESTED_DELETE`、`audit_hidden = 1`。
- 默认检索、探索中心、MCP、混合检索都排除 `audit_hidden=1` 的卡片。
- 必须提供“建议删除的知识卡片”筛选入口，用户可以搜索、查看、恢复。
- 只有用户原有的手动删除功能可以物理删除；审计导入永远不物理删除。

### 2.2 用户已确认卡片保护

- `is_user_verified = 1` 的卡片：
  - 审计记录照常保存并显示；
  - **不自动应用修改、不自动隐藏、不自动删除**；
  - 修改/删除建议进入“待人工确认”状态；
  - UI 必须明确显示“用户已确认，审计建议未自动应用”。
- 该规则优先于外部 AI 的任何建议。

### 2.3 导入零 Token

- 导入 JSON、应用修改、新增卡片、标记删除、重算共识/冲突，全部为本地操作。
- 导入期间禁止调用 `ModelGateway` 的 LLM / VLM / 网页 AI。
- 修改/新增卡片产生的向量更新：
  - 若 embedding 角色是本地 ONNX：可以后台重建，不产生 Token；
  - 若 embedding 角色是云端 API：只标记 `needs_vector_update=1`，由用户后续手动触发“重建受影响向量”，不得静默消耗 Token；
  - 向量未更新前，FTS/关键词检索仍可用。
- 新卡片的合并/去重只使用现有确定性规则；不调用 LLM 重新提取。

### 2.4 审计独立性

- 默认导出的 `report.md` **不包含已有 AI 评分**，避免外部 AI 被前一次评分锚定。
- 已有审计历史保存在程序的数据库中，并可在 UI 查看；报告导出时可选择是否附带历史，默认关闭。
- 每张卡的生成模型来源可以写入报告（用户要求标注），但不得写入“上次评分/评价”，除非用户显式勾选。

---

## 3. 用户完整流程

### 3.1 第一步：导出文献校对包

在【文献详情】增加按钮：

> **📤 导出 AI 校对包**

导出目录：

```text
workspace/exports/audit/<doc_id>/<YYYYMMDD-HHMMSS>/
├── report.md                 # 人读的完整文献总结报告
├── audit_prompt.md           # 给外部 AI 的严格提示词
├── cards.jsonl               # 机器可读卡片清单（导入匹配用）
├── manifest.json             # report_id / report_hash / 卡片数 / 文件列表
└── README.txt                # 使用说明
```

如果文献卡片数超过阈值（默认 200 张或报告超过约 60,000 字符），自动拆分为：

```text
├── report.md                 # 目录和总说明
├── report_part-01.md
├── report_part-02.md
├── ...
├── audit_prompt.md
├── cards.jsonl
└── manifest.json
```

用户把以下文件发给外部 AI：

1. 原文献 PDF；
2. `report.md`（或全部 `report_part-*.md`）；
3. `audit_prompt.md`。

### 3.2 第二步：外部 AI 校对

外部 AI 返回一个严格 JSON（推荐），或返回包含 JSON 代码块的 Markdown。程序优先接受 JSON 文件；`.md` 文件会尝试提取其中的 fenced JSON。

**格式选择结论：JSON 明显优于纯 Markdown。** 原因：

- 修改、删除、新增需要稳定的字段和层级；
- 必须用 `knw_id` 精确匹配，Markdown 标题容易改名、丢失 ID；
- JSON 可以被 schema 校验，能在写库前拒绝坏数据；
- 多批次、多个 AI 的结果可以合并去重；
- Markdown 只适合人工阅读，程序仍应从 JSON 生成 Markdown 预览。

### 3.3 第三步：导入审计结果

在【文献详情】增加按钮：

> **📥 导入 AI 校对结果**

导入对话框：

1. 选择 JSON/Markdown 文件；
2. 选择或填写审计 AI 身份：
   - 从“统一模型与算力池”节点选择：自动带出节点名、模型、Provider、节点 ID；
   - 手动填写：AI 名称必填，模型名/Provider 可选；
   - 审计来源标记为 `manual_import` 或 `pool`；
3. 程序匹配 `report_id` / `report_hash` / `knw_id` / `content_hash`；
4. 显示预览：
   - 保留、修改、建议删除、新增、无效、过期、用户已确认冲突；
   - 每张卡的当前内容 vs 修改后内容 diff；
   - 评分与评级；
5. 用户可选择“全部应用”或逐条勾选；
6. 应用后显示结果统计，并跳转到相关卡片或“建议删除”筛选。

### 3.4 第四步：查看与恢复

- 卡片红色标记“建议删除”，显示审计 AI、评分、理由。
- 探索中心/文献详情的卡片筛选增加：
  - **正常卡片**
  - **建议删除的知识卡片**
  - **AI 审计新增**
  - **审计分歧**
  - **全部**
- 默认显示“正常卡片”，不含建议删除。
- 打开建议删除卡片，提供：
  - **恢复为正常卡片**（清空 `audit_hidden` / `audit_delete_suggested`，保留审计历史）；
  - **查看审计详情**；
  - 不提供“审计导入直接永久删除”。

---

## 4. 评分标准（必须逐字实现）

每张知识卡必须有且只有一个 `overall_score`（0–100 整数）和对应评级。**这是单张卡片的总体评价，不是整篇报告的评价。**

| 分数区间 | `verdict` |
|---|---|
| 90–100 | 完全相符 |
| 70–89 | 基本相符 |
| 50–69 | 大致相符 |
| 30–49 | 明显差异 |
| 10–29 | 基本否定 |
| 0–9 | 全盘否定 |

规则：

- `overall_score` 必须是整数；
- `verdict` 必须与分数区间一致；如外部 AI 写错，程序以分数重新推导，并记录一条 `validation_warning`；
- 不允许报告级总体评分；顶层只能有统计数量，不能有 `report_score`；
- 如果证据不足，仍给出 50–69 之间的分数，并设置 `needs_manual_review=true`、`suggested_action="keep"`，不得自动删除或大改；
- 每个 `card_review` 必须带 `knw_id` 和 `content_hash`。

---

## 5. `report.md` 报告规范

### 5.1 文档头

```markdown
---
schema: acdb.document_audit_report/v1
report_id: AUD-<doc_id>-<timestamp>
report_hash: <sha256>
document_id: DOC-...
title: ...
authors: ...
journal: ...
pub_year: ...
doi: ...
generated_at: 2026-11-06T...
card_count: 1306
part: 1
part_total: 7
card_ids_in_part:
  - KNW-...
  - KNW-...
---

# <文献标题> · 知识卡审计报告

## 文献信息
...
```

### 5.2 每张卡片必须包含

````markdown
## 卡片 KNW-...

- knw_id: KNW-...
- content_hash: <sha256>
- card_level: L1 / L2
- card_type: STRAT / MODEL / CODE / PITFALL / CONC
- 标题（中）:
- 标题（英）:
- 摘要:
- 前提与边界:
- 元素 / Z / 离子态 / 等电子序列:
- 方法 / 程序 / 物理过程:
- 结论标签:
- 公式 LaTeX:
- 代码片段:
- 排错提示:
- 来源章节 / 页码:
- 支持文献 ID:
- 检索载荷:
- 生成模型来源:
  - 快速提取: <node/model 或 手动/未知>
  - 深度归纳: <node/model 或 未使用>
  - 图像理解: <node/model 或 未使用>
  - 向量模型: <node/model>
  - 重排模型: <node/model>
- 已有审计: <默认不输出，除非导出时勾选>
````

`cards.jsonl` 每行一张卡，字段与报告一致，供导入时做内容指纹匹配。

### 5.3 报告必须覆盖全部卡片

- 使用 `cards_for_document(doc_id)`，不能用 Explore 的 30 条分页；
- 包括 `SUGGESTED_DELETE` / `audit_hidden=1` 的卡片，并标注状态；
- 包括 L1 总纲与 L2 卡片；
- 默认按 `card_level`（L1 在前）和 `knw_id` 稳定排序；
- 报告中的 `knw_id` 不得被截断、翻译或改写。

### 5.4 大文献分批

- 单份报告默认最多 200 张卡片，超过则自动拆分；
- 所有分片共用同一个 `report_id` 和完整 `report_hash`；
- 每个分片有 `part`、`part_total`、`card_ids_in_part`；
- 外部 AI 对每个分片分别输出 JSON，`batch_id` 对应分片；
- 导入时允许多选多个 JSON 文件，合并为一个审计批次；
- 未覆盖到的 `knw_id` 必须在预览中显示为“未审计”，不得视为通过。

---

## 6. `audit_prompt.md` 提示词规范

提示词必须让外部 AI 明确：

1. 你是计算原子物理审稿人，不是改写编辑；
2. 只依据 PDF 与报告，不得用外部知识臆造；
3. 逐张卡给出 0–100 分与六档评级；
4. 每张卡只允许一个 `suggested_action`：`keep` / `modify` / `delete`；
5. 新增卡片放在顶层 `new_cards`，不能伪装成某张旧卡的修改；
6. 所有判断要有证据定位：页码/章节/短引文；
7. 输出严格 JSON，不输出 Markdown 围栏或额外解释；
8. 不得修改 `knw_id`、`card_level`、`supporting_doc_ids`；
9. 不得给整篇报告一个总体评分；
10. 证据不足时 `needs_manual_review=true`，动作必须 `keep`；
11. 只处理本次报告/分片中列出的 `knw_id`；
12. 不能遗漏任何卡片；不能编造不存在的 `knw_id`。

提示词模板（导出时填入占位符）：

```text
你是计算原子物理（MCDHF / RCI / RMBPT / CC / CI+MBPT / GRASP / FAC / AMBiT / DIRAC）
领域的严格审稿人。下面给你：
1) 原始文献 PDF；
2) 从该文献提取的知识卡报告；
3) 本提示词。

你的任务不是重写整篇文献，而是逐张检查知识卡是否正确、完整、前提是否成立、
公式/代码/物理结论是否与 PDF 一致。

对每张卡：
- 给出 overall_score（0-100 整数）；
- 给出六档 verdict：完全相符 / 基本相符 / 大致相符 / 明显差异 / 基本否定 / 全盘否定；
- 给出 confidence（0-1）；
- 列出 issues（字段、问题、证据页码/章节/短引文）；
- 选择 suggested_action：keep / modify / delete；
- modify 时给出 revised_card（只填需要修改的字段，程序会按字段合并）；
- delete 时给出 delete_reason 和证据；
- 如果证据不足，needs_manual_review=true，suggested_action=keep。

如果 PDF 中出现了报告没有覆盖、但确实重要的知识，请放在顶层 new_cards，
每条给 temp_id、reason、evidence 和完整 card 草稿。

只输出一个严格 JSON 对象，不要 Markdown 围栏，不要解释文字。
不要给整篇报告打分，只给每张卡打分。
不要修改 knw_id、card_level、supporting_doc_ids，不要编造 ID。
只处理本次报告/分片中出现的 knw_id。
```

---

## 7. 外部审计 JSON Schema

### 7.1 顶层结构

```json
{
  "schema": "acdb.audit.v1",
  "document_id": "DOC-PAP-0000-077BFD",
  "report_id": "AUD-DOC-PAP-0000-077BFD-20261106-120000",
  "report_hash": "sha256:...",
  "batch_id": "part-01",
  "auditor": {
    "name": "用户可读名称，例如 GPT-5 审稿",
    "model": "模型名（可空）",
    "provider": "openai / anthropic / google / 手动",
    "node_id": "如果从算力池选择则有值"
  },
  "card_reviews": [
    {
      "knw_id": "KNW-...",
      "content_hash": "sha256:...",
      "overall_score": 82,
      "verdict": "基本相符",
      "confidence": 0.78,
      "needs_manual_review": false,
      "issues": [
        {
          "severity": "minor",
          "field": "premises_and_conditions",
          "message": "缺少对 Breit 相互作用的边界说明。",
          "evidence": {
            "page": 12,
            "section": "2. Computational details",
            "quote": "The Breit interaction was included perturbatively."
          }
        }
      ],
      "suggested_action": "modify",
      "revised_card": {
        "premises_and_conditions": "适用于 ...；Breit 以微扰方式加入。"
      },
      "delete_reason": "",
      "evidence": [
        {
          "page": 12,
          "section": "2. Computational details",
          "quote": "..."
        }
      ]
    }
  ],
  "new_cards": [
    {
      "temp_id": "NEW-01",
      "reason": "PDF 中有一条报告未覆盖的收敛策略。",
      "evidence": [
        {
          "page": 20,
          "section": "3.2",
          "quote": "..."
        }
      ],
      "card": {
        "card_type": "STRAT",
        "title_zh": "...",
        "title_en": "...",
        "summary_zh": "...",
        "premises_and_conditions": "...",
        "element": "Fe",
        "z_num": 26,
        "ion_stage": "Fe XVII",
        "isoelectronic": "Ne-like",
        "method": "MCDHF",
        "code_name": "GRASP2018",
        "physical_process": "E1_transition",
        "formulas_latex": "",
        "code_snippet": "",
        "pitfall_warning": "",
        "keywords": ["..."],
        "source_section": "3.2",
        "page_num": 20
      }
    }
  ]
}
```

### 7.2 字段约束

- `schema` 必须为 `acdb.audit.v1`；
- `document_id` / `report_id` / `report_hash` 必须与导出包一致；
- `auditor.name` 必填；缺失时导入对话框要求用户填写；
- `card_reviews` 中每个 `knw_id` 只能出现一次；
- `revised_card` 只允许修改内容字段，不允许修改：
  - `knw_id`
  - `card_level`
  - `supporting_doc_ids`
  - `is_user_verified`
  - 生成来源/审计历史
- `new_cards` 只允许生成 L2 卡片；新增 L1 总纲必须由用户后续手动触发；
- `overall_score` 与 `verdict` 不一致时，以分数为准重新推导并记录警告；
- 所有 `delete` 必须有 `delete_reason`，否则降级为 `modify` 或 `keep` 并由用户复核；
- 所有 `modify` 必须有至少一个 `revised_card` 字段；
- 单文件大小、卡片数、新增卡片数必须有上限（建议 50 MB / 5,000 卡 / 200 新增），超过时拒绝并提示拆分。

---

## 8. 数据模型与迁移

`schema_ver` 从 2 升级到 3。迁移必须幂等，并兼容旧数据库。

### 8.1 `knowledge_cards` 新增字段

| 字段 | 类型 | 含义 |
|---|---|---|
| `content_hash` | TEXT | 规范化后可编辑字段的 SHA256，用于检测导出后是否被改过 |
| `audit_status` | TEXT | `NORMAL` / `SUGGESTED_DELETE` / `AI_ADDED` |
| `audit_delete_suggested` | INTEGER | 0/1，任一审计建议删除即为 1 |
| `audit_hidden` | INTEGER | 0/1，默认检索过滤条件 |
| `audit_delete_reason` | TEXT | 最近一次删除建议的理由 |
| `audit_delete_by` | TEXT | 最近一次建议删除的审计 AI 名称 |
| `audit_delete_at` | TEXT | 时间 |
| `audit_review_count` | INTEGER | 已导入审计次数 |
| `audit_positive_count` | INTEGER | 评分 ≥ 70 的审计数 |
| `audit_negative_count` | INTEGER | 评分 < 50 的审计数 |
| `audit_score_min` | INTEGER | 多模型最低分 |
| `audit_score_max` | INTEGER | 多模型最高分 |
| `audit_divergence` | INTEGER | 0/1，是否评价分歧 |
| `needs_vector_update` | INTEGER | 0/1，修改/新增后待重建向量 |
| `source_kind` | TEXT | `EXTRACTION` / `AI_AUDIT_ADDED` / `USER` |

### 8.2 `card_model_provenance`

记录每张卡生成时使用的模型，按角色一行：

```text
knw_id, role, node_id, node_name, node_type, provider, model,
web_preset, web_model, recorded_at, source
```

`role` 取值：

- `fast_llm`
- `slow_llm`
- `vlm`
- `embedding`
- `reranker`

规则：

- 提取/重修时快照当前角色绑定的节点，而不是卡片查看时再动态读取；
- 网页 AI 节点记录 `web_model`、`web_preset`、`node_name`；
- 本地 ONNX 节点记录模型名和节点名；
- 半自动剪贴板/手动流程记录为“用户手动”或“外部网页 AI”，由用户补充名称；
- 不记录 API Key、Cookie、代理密码。

### 8.3 `card_audit_reviews`

```text
review_id, knw_id, doc_id, report_id, report_hash, batch_id,
auditor_name, auditor_model, auditor_provider, auditor_node_id,
audit_source, overall_score, verdict, confidence, needs_manual_review,
suggested_action, issues_json, revised_card_json, delete_reason,
raw_json, imported_at, applied_at, status, validation_warning
```

`status`：`IMPORTED` / `APPLIED` / `REJECTED` / `STALE` / `NEEDS_REVIEW`。

同一份 `report_hash + knw_id + auditor_name` 重复导入必须幂等，不重复插入。

### 8.4 `card_revisions`

保存审计修改前后的完整快照：

```text
revision_id, knw_id, audit_review_id, before_json, after_json,
created_at, applied_by
```

用于追溯和恢复；用户已确认卡片的修订只记录，不自动应用。

### 8.5 `document_audit_exports`

```text
report_id, doc_id, report_hash, schema, card_count, part_count,
exported_at, export_dir, manifest_json
```

### 8.6 `audit_import_batches`

```text
batch_id, report_id, auditor_name, file_name, file_hash,
imported_at, applied_at, stats_json
```

用于幂等和导入历史。

---

## 9. 导入与回写流程（不调用 LLM）

### 9.1 解析与校验

1. 读取 JSON；若是 `.md`，提取 fenced JSON；
2. 校验 `schema`、`document_id`、`report_hash`；
3. 校验每个 `knw_id` 存在且属于该文献；
4. 校验 `content_hash` 是否与当前卡片一致：
   - 一致：可应用；
   - 不一致：标记 `STALE`，进入人工复核，禁止自动覆盖；
5. 校验评分/评级/动作；
6. 对 `new_cards` 做字段校验和重复检测。

### 9.2 修改卡片

- 只更新 `revised_card` 中出现的允许字段；
- 保留 `knw_id`、`card_level`、`supporting_doc_ids`、审计历史、生成来源；
- 重新生成 `search_payload`（不能直接信任外部 AI 的检索载荷）；
- 写入 `card_revisions`；
- 设置 `needs_vector_update=1`；
- 更新 `content_hash`；
- 更新 `updated_at` 和“最后修改来源=AI 审计”。

### 9.3 建议删除

- 不物理删除；
- 设置：
  - `audit_status = SUGGESTED_DELETE`
  - `audit_delete_suggested = 1`
  - `audit_hidden = 1`
  - 删除理由/审计 AI/时间
- 保存审计记录；
- 更新 Qdrant payload（或检索时后置过滤）；
- UI 红色标记；
- 默认搜索排除；
- 用户恢复时：
  - 清空 `audit_hidden` / `audit_delete_suggested`；
  - 保留审计历史；
  - 记录一条 `RESTORE` 事件；
  - 恢复向量检索（必要时标记待重建）。

### 9.4 新增卡片

- 只接受 L2；
- 生成新的 `knw_id`；
- `source_kind = AI_AUDIT_ADDED`；
- `is_user_verified = 0`；
- 写入审计来源和 `new_cards` 理由；
- 运行现有确定性去重/合并：
  - 若与现有卡片重复：不新增，合并证据与审计记录，标记原卡片；
  - 若不重复：插入；
- `needs_vector_update=1`，不自动调用云端 embedding。

### 9.5 用户已确认卡片

- `is_user_verified=1`：
  - 不自动修改；
  - 不自动隐藏；
  - 修改/删除建议写入 `status=NEEDS_REVIEW`；
  - UI 显示“用户已确认，需人工处理”；
  - 用户可以在审计预览中显式勾选“仍然应用”，二次确认后才修改/隐藏。

### 9.6 共识与冲突重算（确定性）

应用完成后：

1. 收集所有受影响卡片（修改、新增、恢复、建议删除）；
2. 按元素 / Z / 等电子序列 / 方法 / 物理过程 / 卡片类型分组；
3. 用现有 `ConsensusConflictEngine` 做确定性合并与冲突检测；
4. 更新：
   - `consensus_count`
   - `supporting_doc_ids`
   - `has_conflict`
   - `conflict_with_knw_ids`
5. `SUGGESTED_DELETE` 卡片不参与新的冲突组，但保留历史；
6. 不调用 LLM 重新生成 L1 总纲；只标记 `master_stale=1` 或事件，提示用户后续处理；
7. 重新计算多 AI 审计统计字段。

### 9.7 向量与 FTS

- FTS 由现有触发器自动更新；
- 修改/新增卡片标记 `needs_vector_update=1`；
- 本地 ONNX embedding 可在后台重建；
- 云端 embedding 必须等待用户显式操作；
- 建议删除卡片：
  - 不从 Qdrant 物理删除；
  - 更新 payload `audit_hidden=1`；
  - 检索默认 filter 排除；
  - 若 payload 更新失败，检索层按 SQLite 字段后置过滤；
- 恢复时更新 payload 或标记待重建。

---

## 10. UI 设计

### 10.1 文献详情

新增按钮：

- **📤 导出 AI 校对包**
- **📥 导入 AI 校对结果**

导出时显示：

- 卡片总数、分片数；
- 是否包含已有审计历史（默认否）；
- 导出目录；
- “复制文件路径”按钮。

### 10.2 审计导入对话框

- 文件选择；
- 审计 AI 身份：
  - 从算力池节点选择；
  - 手动填写名称/模型/Provider；
- 报告匹配状态；
- 预览统计；
- 逐条 diff / 勾选；
- 应用按钮；
- 明确提示“不会消耗 Token，不会物理删除卡片”。

### 10.3 卡片抽屉

新增：

- 生成来源区：
  - 快速提取
  - 深度归纳
  - 图像理解
  - 向量模型
  - 重排模型
- 审计历史区：
  - AI 名称/模型
  - 分数 + 六档评级
  - 建议动作
  - 时间
  - 理由/证据
- 多模型汇总：
  - “多模型肯定”（≥2 个模型评分 ≥70）
  - “评价分歧”（最高分-最低分 ≥30，或同时存在 ≥70 与 <50）
  - 最近一次评分

### 10.4 探索中心筛选

在卡片筛选区增加“卡片状态”：

- 正常卡片（默认）
- 建议删除的知识卡片
- AI 审计新增
- 审计分歧
- 全部

要求：

- 默认 `audit_hidden=0`；
- 选择“建议删除”时，卡片红色边框/红色徽标，显示删除理由；
- 卡片可打开抽屉并恢复；
- 分页总数和页码同步反映筛选结果。

### 10.5 红色标记

- 卡片边框/左上角徽标使用红色；
- 不要用删除线覆盖正文，保证可读；
- 抽屉顶部显示醒目的“建议删除 · 未物理删除”横幅；
- 恢复按钮二次确认。

---

## 11. 多 AI 评价与分歧规则

每张卡保留所有历史审计，不覆盖旧记录。

派生字段：

- `audit_review_count`
- `audit_positive_count`：`overall_score >= 70`
- `audit_negative_count`：`overall_score < 50`
- `audit_score_min` / `audit_score_max`
- `audit_divergence = 1` 当：
  - `audit_score_max - audit_score_min >= 30`，或
  - 同时存在 `>=70` 与 `<50` 的评分

展示规则：

- ≥2 个模型评分 ≥70：显示“多模型肯定”绿色徽标；
- 存在 delete 建议：显示“建议删除”红色徽标；
- `audit_divergence=1`：显示“评价分歧”橙色徽标；
- 单一模型评分只作为参考，不直接改变卡片正常/隐藏状态，除非该审计的 `suggested_action=delete`。

评价不直接参与检索排序，避免评分影响召回；只作为筛选和人工判断依据。

---

## 12. 检索行为

以下入口默认排除 `audit_hidden=1`：

- Explore 空查询浏览与关键词搜索；
- `HybridRetriever.search()` / `search_page()`；
- MCP 检索工具；
- 文献详情默认关联卡片列表；
- 向量召回、FTS 召回、rerank 输入。

显式筛选“建议删除的知识卡片”时：

- 只返回 `audit_hidden=1`；
- 支持分页、关键词搜索、打开抽屉和恢复；
- 不进入默认 L1/L2 统计。

文献详情显示：

```text
关联知识卡 · 1,306（其中建议删除 12）
```

报告导出默认包含全部卡片，并标注 `audit_status`，以便下一个 AI 继续审计。

---

## 13. 学术争议与策略冲突

审计回写后必须触发确定性重算：

- 修改后的卡片可能从“冲突”变为“一致”，或反之；
- 建议删除的卡片从活跃冲突组移除；
- 新增卡片可能引入新的冲突；
- L1 总纲不自动用 LLM 重写，只标记 stale，等待用户手动/后续任务处理；
- 事件日志记录：
  - `AUDIT_APPLIED`
  - `CARD_MODIFIED`
  - `CARD_SUGGESTED_DELETE`
  - `CARD_RESTORED`
  - `CARD_ADDED_BY_AUDIT`
  - `CONSENSUS_RECALCULATED`

---

## 14. 安全与健壮性

- 外部 JSON 视为不可信输入：
  - 限制文件大小/卡片数/新增数；
  - 严格字段白名单；
  - 不执行任何代码/HTML/脚本；
  - Markdown 渲染转义；
  - 拒绝路径穿越、非法 ID、重复 ID。
- 导入必须在数据库事务中完成；失败回滚。
- 幂等：
  - 同一 `report_hash + card + auditor` 不重复写入；
  - 同一新增 `temp_id` 不重复插入。
- 过期检测：
  - `content_hash` 不匹配时禁止自动改写；
  - 提示用户重新导出/重新审计。
- API Key / Cookie 不进入报告、不进入审计记录、不进入日志。

---

## 15. 测试与验收

### 15.1 单元测试

1. 报告导出包含文献全部卡片，而不是 30 张；
2. 报告包含稳定 `knw_id`、`content_hash`、生成模型来源；
3. 超过阈值自动分片，分片合并后卡片不重复、不遗漏；
4. 评分与六档评级映射正确；
5. JSON schema 校验：缺字段、错 ID、错分数、重复 ID 均拒绝；
6. 修改卡片保留 `knw_id` / `card_level` / 来源 / 审计历史；
7. 建议删除只软删除，数据库行仍存在；
8. 默认检索排除建议删除，筛选后能查到；
9. 恢复后重新可检索；
10. 新增卡片去重、来源标记、向量待更新；
11. 用户已确认卡片不被自动修改/隐藏；
12. 多 AI 审计统计和分歧标记正确；
13. 导入同一文件两次幂等；
14. `content_hash` 过期时拒绝自动覆盖；
15. 导入过程没有调用任何 LLM（用 fake gateway 断言零调用）；
16. 冲突/共识重算结果正确。

### 15.2 GUI 测试

1. 文献详情出现导出/导入按钮；
2. 导出目录和文件完整；
3. 导入预览显示修改/删除/新增/无效数量；
4. 应用后卡片内容变化；
5. 建议删除卡片红色、默认不可见、筛选可见、可恢复；
6. 卡片抽屉显示生成模型和审计历史；
7. “多模型肯定/评价分歧/建议删除”徽标正确；
8. 分页总数与筛选联动。

### 15.3 性能验收

- 1,306 张卡片的文献导出不崩溃、不一次性渲染全部卡片；
- 导入 1,000 条审计记录在本地数秒内完成；
- 不触发 Qdrant 锁冲突；
- 不消耗任何 Token。

---

## 16. 交付物

- 数据库 schema v3 迁移；
- 报告导出器；
- 审计提示词模板；
- 审计 JSON 解析/校验/导入器；
- 软删除/恢复/筛选；
- 模型来源与审计历史 UI；
- 多 AI 共识/分歧统计；
- 文档：
  - `UpdateTask_05.md`（本文件）
  - `FixTask_05_01.md`（实现记录）
  - README 更新；
- 测试通过并生成 V2.4.0 的 Patch/Full 发布包。

---

## 17. 非目标

- 不自动调用外部 AI 执行审计（本任务只做导出/导入；未来可另开直连任务）；
- 不自动用 LLM 重写 L1 总纲；
- 不物理删除审计建议删除的卡片；
- 不把审计评分直接用于检索排序；
- 不在导入阶段自动调用云端 embedding。

---

## 18. 一句话验收

用户能对任意文献一键导出包含全部知识卡的 Markdown 报告和严格 JSON 提示词，交给外部 AI 审计后，把 JSON 导回程序；程序在不消耗 Token、不物理删除、不覆盖用户确认卡片的前提下，完成修改、软删除、新增、模型来源标注、多 AI 评价记录、共识/冲突重算，并让“建议删除”卡片在默认检索中隐藏、在专用筛选中可查、可恢复。
