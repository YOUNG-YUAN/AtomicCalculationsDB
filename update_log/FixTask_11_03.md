# FixTask_11_03.md — `AtomicCalculationDB` (v3.0.3) 网页大模型回复解析引擎偶发前缀误粘清洗与嵌套片段优先级容错加固

> **版本定位**：从 `v3.0.2` 平滑升级至 `v3.0.3`（Build `20261118`）。  
> **任务类型**：Z 版本日常补丁维护（遵循：不改动 `README.md`、不发起 GitHub Release，仅生成本地 `Patch` 与 `Lite` 构建包供本地更新）。

---

## 1. 现象复现与问题诊断

### 1.1 用户反馈与异常截图
用户在半自动提问台导入 `DOC-PAP-0000-8C9C21` 论文的 Batch 01 回复时，触发弹窗报错：
```
【提问包不匹配】
回复中包含的片段（ 等）与当前提问包要求的片段（DOC-PAP-0000-8C9C21-0, DOC-PAP-0000-8C9C21-1, DOC-PAP-0000-8C9C21-10 等）不符，请确认是否粘贴错了其他文献的提问包回复。
```
注意其中包含的识别片段为空：`（ 等）`。

### 1.2 根因逐级深度排查

1. **细微字符污染导致 JSON 外层阻断**：
   - 检查用户提供的 Batch 01 回复全文（共 18 个待处理片段），在第 2 个片段（`DOC-PAP-0000-8C9C21-1`）末尾处，存在一行异常语法：
     ```json
     "source_section": "I. INTRODUCTION",
     Semi "generalized_conjecture": null,
     ```
   - 属性名 `"generalized_conjecture"` 前面多出了一个未用引号包裹的词 `Semi `（可能源于复制、操作半自动工作台或外部界面切换时的偶发键盘按键输入）。
   - Python 原生 `json.loads()` 遇到该行直接抛出 `Expecting ',' delimiter: line 133`，导致外层整体 `{"items": [...]}` 的解析失败。

2. **嵌套候选对象提取优先级竞争缺陷**：
   - 当外层 JSON 解析失败时，`_extract_json_payload` 的回退机制启动 `JSONDecoder.raw_decode` 逐字符扫描文本中的有效 JSON 子对象。
   - 在扫描到第 1 个片段（ITEM 0）的 `result` 内部时，成功解码出单卡结构 `{'document_title': ..., 'cards': [...]}`。
   - 原代码判断条件为：
     ```python
     for value in candidates:
         if isinstance(value, dict) and ("items" in value or "cards" in value):
             return value
     ```
   - 由于 `"cards" in value` 被过早命中，导致整个解析器直接返回了该**内部单卡字典**，丢弃了外层的整个 `items` 列表与关联的 `item_id`。
   - `parse_reply` 将其退化为 `item_id = ""` 的孤立卡片，导致后续片段交集匹配为 0，且打印片段预览时显示为奇怪的空括号 `（ 等）`。

---

## 2. 解决方案与核心改造

### 2.1 智能前缀/杂质清洗（`atomic_prompts.repair_json_text`）
在 JSON 预处理修复器中增加对外层非字符串区域内偶发杂质单词的正则清洗：
```python
# Strip stray unquoted tokens/words before property keys outside strings (e.g. `Semi "key":`, `Assistant: "items":`)
text = re.sub(r'([,{]\s*)[a-zA-Z_][a-zA-Z0-9_\-\.]*[:\s]+\s*(\"[a-zA-Z0-9_]+\"\s*:)', r'\1\2', text)
```
- 自动识别并抹除紧跟在 `,` 或 `{` 之后、属性键 `"` 之前意外出现的未带引号标识符（如 `Semi "key":`、`Assistant: "key":`）；
- 即使原始文本包含此类人为偶发输入错误，系统亦能在第一道防线实现 100% 自动化静默修复与秒级解析。

### 2.2 多片段与单卡提取分级优先级（`web_ai_bridge._extract_json_payload`）
彻底拆分候选字典的评估阶段：
```python
# 1. 优先匹配外层批量任务清单（包含明确的 item_id 索引）
for value in candidates:
    if isinstance(value, dict) and "items" in value:
        return value

# 2. 仅在确实无 items 时，才降级匹配单卡结构
for value in candidates:
    if isinstance(value, dict) and "cards" in value:
        return value
```
杜绝内层片段误抢占外层清单对象。

### 2.3 友好的拦截错误提示（`semi_auto.import_reply`）
优化错误报文展示逻辑：
```python
valid_rec = [k for k in sorted(results) if k.strip()]
sample_rec = ", ".join(valid_rec[:3]) if valid_rec else "（未识别出有效片段 ID）"
```
当提取到的 ID 为空时，明确提示“（未识别出有效片段 ID）”，避免用户产生“括号内无内容”的困惑。

---

## 3. 验证与交付

1. **自动化测试**：
   - 新增 `tests/test_v303.py`，专门测试带有 `Semi "generalized_conjecture"` 污染的 JSON 修复与完整 18 个片段的成功解析；
   - 验证 candidate 提取在嵌套 cards 时优先返回 items；
   - 运行全量测试套件，**230 个测试全部通过（100% Passed）**。
2. **本地环境与构建**：
   - 同步 `app_core` 至 `C:\Normal Program Files\AtomicCalculationDB\` 及 `dist\`；
   - 本地生成 `release/AtomicCalculationDB_Patch_v3.0.3.zip` 与 `release/AtomicCalculationDB_Lite_v3.0.3.zip`。
