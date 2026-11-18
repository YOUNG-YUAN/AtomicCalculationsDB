# UpdateTask_12.md — `AtomicCalculationDB` (v3.0.1) 网页大模型回复解析引擎容错加固与提示词规范优化

> **版本定位**：从 `v3.0.0` 正式平滑升级至 `v3.0.1`（Build `20261118`）。
> **核心目标**：针对真实场景下 Web AI（如 GPT-6.1 / Claude 等）返回的含未转义多行控制字符、单行混排多键值对、嵌套字典型物理延拓 (`generalized_conjecture`) 以及中文卡片类型（如“理论交叉验证”、“精度诊断”）等非标格式，对底层 JSON 提取引擎与 Pydantic 数据契约进行全面容错加固，并同步升级 Prompt 提示词规范。

---

## 1. 故障根因深度诊断与分析

用户在处理 W XXXVIII（DOC-PAP-0000-83DD88）文献的高激发奇宇称能级寿命及跃迁数据提炼时，网页 AI 给出了高质量的物理提炼结果，但软件在点击“导入回复”时报错 `回复格式错误：回复中没有可解析的 ExtractionResult`。经源码级逐步回溯发现由以下 4 个复合因素导致：

1. **RFC 8259 控制字符导致 `json.loads` 崩溃**：
   - AI 在 `"document_title"`、`"summary_zh"` 等长文本中直接包含了未转义的真实换行符 `\n`。
   - 原代码调用 `json.loads(body)` 与 `json.JSONDecoder()` 时采用默认 `strict=True`，遇未转义换行符直接抛出 `JSONDecodeError: Invalid control character at line 1 column 77`。
2. **`repair_inner_quotes` 贪婪匹配破坏单行混排 JSON**：
   - 当大模型在同一行中紧凑输出多个键值对时（如 `"metadata_source": "出版社文献记录", "items": [ { "item_id": "DOC-PAP-0000-83DD88-27",`），原单行引号修复正则 `^(\s*"[a-zA-Z0-9_]+"\s*:\s*")(.*)("(?:\s*,)?\s*)$` 中的 `.*` 会产生跨字段贪婪匹配。
   - 这导致其将 `"items":`、`"item_id":` 误当做内部未转义的双引号进行加斜杠转义（变成 `\"items\":`），从而彻底破坏了 JSON 顶层语法结构。
3. **`generalized_conjecture` 类型漂移引发 Pydantic 校验拒绝**：
   - 大模型将广义物理延拓结构化为字典输出：
     ```json
     "generalized_conjecture": {
       "statement_zh": "若某激发态的 E1 总衰变率受到强烈抑制...",
       "physical_basis_zh": "总辐射寿命由所有向下辐射通道速率之和决定...",
       "boundary_zh": "这是对其他能级的条件性延拓..."
     }
     ```
   - 底层 `KnowledgeCardDraft.generalized_conjecture` 期望 `str | None`。原 `_sanitize_card_dict` 虽然将该字典复制进了 `structured_details`，但未对其自身做字符串降维，导致抛出 `pydantic_core.ValidationError: Input should be a valid string`。
4. **非标/中文 `card_type` 别名未对齐**：
   - AI 输出带有特定中文描述（如 `"card_type": "理论交叉验证"`、`"精度诊断"`、`"理论与实验基准比较"`）。原别名映射表仅覆盖部分英文缩写，未将包含“验证/诊断/比较/基准”的中文标签平滑映射为规范的 `CONC`。

---

## 2. 核心架构与解析机制优化

### 2.1 RFC 8259 宽松解码器升级 (`strict=False`)
在 `app_core/models/web_ai_bridge.py` 中的 `_extract_json_payload` 函数中：
- 所有 `json.loads(..., strict=False)` 与 `json.JSONDecoder(strict=False)` 均开启宽松模式；
- 原生允许字符串内部包含实际的非转义换行符 `\n`、制表符 `\t` 等控制字符，避免直接报错。

### 2.2 防误伤的单行引号安全修复 (`repair_inner_quotes`)
在 `app_core/models/atomic_prompts.py` 中：
- 若文本已经能被 `json.loads(text, strict=False)` 正常解析，则跳过修复，绝不二次篡改；
- 行级扫描时增加结构化守卫条件：只要该行出现多个键值对标识（`line.count('":') > 1`、`": [`、`": {` 或 `",\s*"`），坚决不执行贪婪替换，彻底杜绝误转义结构引号的隐患。

### 2.3 `generalized_conjecture` 结构化智能缝合
在 `KnowledgeCardDraft` 模型中新增前置字段校验器 `@field_validator("generalized_conjecture", mode="before")`，并在 `_sanitize_card_dict` 中做同步增强：
- 遇到字典结构时，自动提取 `statement_zh`、`physical_basis_zh` 和 `boundary_zh`，以高可读性的结构拼接为纯文本：
  ```
  若某激发态的 E1 总衰变率受到选择定则或跃迁振幅抵消的强烈抑制...
  【物理基础】: 总辐射寿命由所有向下辐射通道的速率之和决定...
  【适用边界】: 这是对其他能级的条件性延拓...
  ```
- 同时在 `structured_details` 中完整保留原始字典的 JSON 字符串，兼顾检索排版与数据精度。

### 2.4 扩展中文与描述性 `card_type` 自动归一化
在 `KnowledgeCardDraft._normalize_card_type` 与 `_sanitize_card_dict` 中全面支持中文原子物理领域标签映射：
- 包含 `策略` / `方法` / `流程` / `METHOD` $\rightarrow$ `STRAT`
- 包含 `模型` / `势函数` / `算符` / `MODEL` $\rightarrow$ `MODEL`
- 包含 `代码` / `程序` / `算法` / `公式` / `CODE` $\rightarrow$ `CODE`
- 包含 `避坑` / `陷阱` / `失效` / `PITFALL` $\rightarrow$ `PITFALL`
- 包含 `结论` / `诊断` / `比较` / `基准` / `验证` / `CONC` / `BENCHMARK` $\rightarrow$ `CONC`

---

## 3. Prompt 提示词规范强化

在 `CARD_SYSTEM_PROMPT`（以及网页半自动提问包输出要求）中进行了如下明晰化补充：
1. **公式双反斜杠强调**：强调 `formulas_latex` 中所有 LaTeX 宏必须采用双反斜杠（如 `\\frac`, `\\delta`, `\\alpha`），严禁输出裸反斜杠；
2. **规范卡片类型提示**：明确提示五大标准枚举类型及适用范畴；
3. **广义物理延拓说明**：指明 `generalized_conjecture` 可采用包含【物理基础】与【适用边界】的纯字符串格式输出；
4. **单行排版提示**：引导模型遵循标准 JSON 换行排版，减少单行混排概率。

---

## 4. 测试与验证

- 新建 `tests/test_v301.py` 测试套件，包含对用户提供的实际 W XXXVIII JSON 数据的端到端解析验证；
- 全量运行 209 个单元测试，**209 passed in 33.5s (100% 通过)**。
