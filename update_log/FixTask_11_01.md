# FixTask_11_01.md — `AtomicCalculationDB` (v3.0.1) 网页大模型回复解析引擎容错加固与提示词规范优化

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

---

## 5. 提问包与上下文表格行截断（同一行截断）根因确认与深度修复

### 5.1 故障真实性深度确认（100% 属实）
用户反馈在提问包/上下文预览中观察到表格同一行被截断（如末尾出现 `27    M1    1.` 且后续数据列缺失）。
经反向追查真实环境与文献数据：
- **文献定位**：该截图真实来源于文献 `wang2016.pdf` (ApJ Suppl 2016, 223:3，对应 Fe XX 跃迁能级与速率数据) 的 Table 7，出现在 `Bundle 58`。
- **PDF 物理分页原状**：截断位置严格发生在原 PDF 第 30 页最底边缘处。原文该行完整数据应为：
  `27 | 2s^2 2p^3 ^2P_1/2 - 2s^2 2p^3 ^4S_3/2 | M1 | 1.643E-01 | 1.641E-01 | 1.700E-01 | -0.1 | 3`
- **MinerU OCR 跨页裁切缺陷**：MinerU 在跨页检测表格边框时，下边缘裁切框穿过了第 30 页最后一行文字，识别出残缺浮点数 `1.` 并将后续 5 个单元格填充为空：
  `<tr><td>27</td><td>...</td><td>M1</td><td>1.</td><td></td><td></td><td></td><td></td></tr>`
  第 31 页续表则从下一行 `27 | 2s^2 2p^3 ^2P_3/2 - 2s^2 2p^3 ^2D_5/2` 继续。
- **问题严重性**：残缺行穿透至 `parsed.md`、`focal_window_markdown` 并进入提问包，导致大模型接收到被腰斩的数据（`1.` 被误当为数值），干扰物理逻辑并可能引发大模型回复拒识。

### 5.2 潜在引发截断的双重成因分析
1. **成因 A（主要根源：OCR 页面边缘残缺行穿透）**：
   原有流程对 HTML 表格与 Markdown 表格缺乏对“分页裁切残损行”的防御性校验，盲目将含 `1.` 和空列的残缺行转为管道表格；
2. **成因 B（次要隐患：`DynamicBatchPacker` 的定长硬字符切分）**：
   在 `batch_packer.py` 的 `_split_by_characters` 中，原实现使用固定字符步长 `text[index:index + size]` 进行切片。若遇到超长单行或表格文本时，会直接在任意字符偏移处“腰斩”Markdown 表格行，造成同一行切断。

### 5.3 四层防御体系与解决方案落地

#### 1. 残缺表格行识别器 (`app_core/parser/html_table.py`)
实现 `is_truncated_table_row(row, max_cols)` 与 `is_truncated_markdown_table_line(line, expected_cols)`：
- 精确检测浮点数残损格式（如 `^[+-]?\d+\.$` 匹配 `1.`，`^[+-]?\d+(?:\.\d*)?[eE][+-]?$` 匹配 `1.2E+` 等非首列残缺数值）；
- 检测跨单元格未闭合的 LaTeX 数学标记（`count('$') % 2 != 0`）；
- 检测末尾严重列丢失（丢失 $\ge 50\%$ 列）且以断裂标点结尾的残损行。

#### 2. HTML 转 Markdown 自动剪除 (`html_table.py`)
在 `_grid()` 和 `html_table_to_markdown()` 中，生成网格与单元格映射后，自动检测并剪除底部被跨页裁切的残损行，保证 `parsed.md` 从源头不被污染。

#### 3. 表格压缩与上下文拼接双重净化 (`table_compressor.py` & `context_bundler.py`)
- 在 `compress_markdown_table()` 中增加对 Markdown 表格末尾残缺行的修剪；
- 在 `context_bundler.py` 生成 `focal_window_markdown` 时，调用 `sanitize_markdown_table_text(window)`，即使面对已解析的旧文献，也能在组装 Bundle 时自动剔除残损行。

#### 4. `DynamicBatchPacker` 换行符对齐切分 (`batch_packer.py`)
全面重构 `_split_by_characters`：切分时在切片候选位置自动向前回溯查找 `\n`；若无则在合理容差范围内向后搜索 `\n`。保证分块永远在完整行边界处换行，彻底消除 Markdown 表格同一行被腰斩的技术隐患。

### 5.4 验证结果
- 新增针对残损行判定、HTML/Markdown 表格修剪、正文表清洗及分包换行对齐的 5 个单元测试；
- 全工程单元测试通过总数提升至 **215 passed (100% 通过)**。

---

## 6. Gemini 4 Argon High 回复解析诊断与卡片标题自动生成兜底

### 6.1 用户测试案例与根因剖析
用户将 `DOC-PAP-0000-D708BD` 的提问包发送给 Google AI Studio 上的 Gemini 4 Argon High，模型返回了高质量提取结果，但在旧版 `v3.0.0` 中无法导入。深度对照发现由以下原因导致：
1. **单斜杠非法 LaTeX 转义**：Gemini 输出了包含单反斜杠的 LaTeX 宏（如 `\geqslant`、`\;`、`\rightarrow`）。在标准 JSON 语法中，`\g` 和 `\;` 为非法转义序列，导致原生 `json.loads`（即使开启 `strict=False`）直接报 `JSONDecodeError: Invalid \escape`；
2. **文本内嵌原生硬换行**：在 `document_title` 等字段中包含多行硬换行，在 `strict=True` 下触发 `Invalid control character` 崩溃；
3. **卡片标题 (`title_zh`) 遗漏**：Gemini 输出了 20 多个字段，却唯独遗漏了卡片标题 `title_zh` / `title`，且将 `document_title` 等嵌套放在了各 item 的 `result` 内部。

### 6.2 修复与兜底保障措施
1. **多重转义自动修复**：`v3.0.1` 中的 `repair_json_text` 预处理器能自动扫描并安全修补所有的单反斜杠 LaTeX 非法转义符与内嵌换行，实测该 JSON 在 `v3.0.1` 中已能 100% 成功解析；
2. **卡片标题智能合成兜底**（`app_core/models/web_ai_bridge.py`）：
   - 当大模型漏输 `title_zh` 与 `title` 时，自动提取 `summary_zh` 中首句物理结论（过滤换行后截取前 45 字符）生成紧凑标题；
   - 若无总结则以 `{ion_stage} {physical_process} 提炼卡片` 兜底，坚决杜绝空标题卡片入库；
3. **新增单元测试**：在 `tests/test_v301.py` 中新增 `test_gemini_argon_high_json_parsing` 回归测试，全量测试用例增至 **216 passed (100% 通过)**。

---

## 7. 写入数据库致命崩溃修复：原子序数（Z）数值溢出与 SQLite INTEGER 上限防护

### 7.1 故障现象与根因诊断（对应“写入失败: Python int too large to convert to SQLite INTEGER”）
用户在半自动提炼弹窗中点击“写入知识库”时，界面卡住并弹出模态错误框：`写入失败: Python int too large to convert to SQLite INTEGER`。

经断点排查真实运行现场与 `batch_25` 数据：
1. **大模型回复格式**：针对文献 `DOC-PAP-0000-D708BD-58`（类氮等电子序列），大模型输出了说明性范围字符串：
   `"z_num": "18-30 (Table 7 涵盖 Z=19, 21, 22, 23, 24, 26, 27；Table 6 涵盖 Z=26)"`；
2. **粗暴提取导致数字爆炸**：原 `_sanitize_card_dict` 中处理代码为：
   ```python
   digits = "".join(c for c in z_val if c.isdigit())
   card["z_num"] = int(digits) if digits else None
   ```
   该逻辑将字符串中出现的所有数字字符一股脑连接成了 `1830719212223242627626`（长达 22 位，数量级达 $1.83 \times 10^{21}$）；
3. **SQLite 64 位有符号整型溢出**：
   - 64 位整数上限为 $2^{63}-1 = 9,223,372,036,854,775,807$；
   - 当 `pipeline._store_draft` 调用 `db.list_knowledge_cards(filters={"z_num": 1830719212223242627626, ...})` 时，`sqlite3` 驱动在绑定参数 `WHERE c.z_num = ?` 处抛出 `OverflowError: Python int too large to convert to SQLite INTEGER`；
4. **伴随排查发现的 `batch_05` 引文跳过问题**：
   在 `batch_05/reply.txt` 的 `search_payload` 中包含未转义引文 `under "Mixing coefficients LSJ": Key 42...`。原 `repair_inner_quotes` 采用 `line.count('":') > 1` 检测多属性，因引文中带冒号而被误判跳过，导致该 batch 曾报 `回复格式错误`。

### 7.2 解决方案与多层纵深防御体系

1. **物理语义归一化函数 `sanitize_z_num`（`parser/card_quality.py`）**：
   - 严格限定原子序数物理合法范围 $1 \le Z \le 250$；
   - 凡是检测到区间连字符（如 `18-30`, `18~30`, `18 to 30`, `18至30`）或多组原子序数，判定为等电子序列卡片，`z_num` 统一归一化为 `None`（序列信息由 `element` / `isoelectronic` 表达）；
   - 仅当明确指定单一原子序数（如 `26`, `Z=26`, `Fe XX (Z=26)`）且在 $[1, 250]$ 内时，才提取为整型，其余一律置 `None`。
2. **年份提取加固（`web_ai_bridge.py`）**：
   - `_sanitize_result_payload` 中的 `document_year` 改用四位年份正则 `re.search(r"\b(19\d\d|20\d\d)\b", y)` 提取，彻底杜绝粗暴数字拼接。
3. **引文引号修复精准识别（`atomic_prompts.py`）**：
   - 重构 `repair_inner_quotes`：只有行内实际出现 `, "<key>":`（逗号分隔的下一属性）或结构符号 `": [`、`": {` 时才跳过；纯文本中出现的 `"Key": Value` 英文引文能够被正确识别并安全转义。
4. **SQLite 底层纵深安全护栏（`sqlite_manager.py`）**：
   - 在 `_card_filter_sql` 中对 `filters.get("z_num")` 进行数值校验，非合法 $[1, 250]$ 整数绝不拼入 SQL 参数绑定；
   - 在 `upsert_knowledge_card` 中安全钳制 `z_num` 为 $[1, 250]$ 或 `None`；
   - 在 `card_hash.py`、`hybrid_retriever.py`、`qdrant_manager.py` 中全量覆盖 `OverflowError` 拦截。
5. **事务回滚与用户恢复**：
   - `finalize_run` 全过程在 `db.transaction()` 保护下进行，报错时已完整自动回滚，数据库没有残留脏数据；
   - 用户在“写入失败”弹窗点击“确定”后，直接在半自动窗口再次点击“写入知识库”即可顺利入库。

### 7.3 验证结果
- 在 `tests/test_v301.py` 中补充 `test_sanitize_z_num_and_overflow_protection` 与 `test_repair_inner_quotes_with_inner_colon_prose` 两个测试；
- 全工程单元测试通过总数提升至 **218 passed (100% 通过)**。
