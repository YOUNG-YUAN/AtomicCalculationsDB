"""Atomic-physics prompt library and strict Pydantic output schemas.

The central prompt is the "cognitive stitching" instruction from
UpdateTask_01 §3.2: resolve pronouns/model labels, bind every conclusion to its
premises, extract formulas/code, and build a bilingual retrieval payload.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from parser.context_bundler import ContextBundle


class PromptParseError(ValueError):
    pass


class KeywordDraft(BaseModel):
    term_en: str
    term_zh: str = ""
    synonyms: list[str] = Field(default_factory=list)
    category: str = ""


class CardConditions(BaseModel):
    """Machine-checkable applicability boundary of one card.

    Empty strings mean the extractor did not report the field.  The model is
    instructed to use ``unknown`` / ``not_applicable`` explicitly when it can
    distinguish those states; keeping them as strings avoids forcing every
    card to invent a numeric value.
    """

    target_quantity: str = ""
    unit: str = ""
    value: str = ""
    uncertainty: str = ""
    state_initial: str = ""
    state_final: str = ""
    transition: str = ""
    j_parity: str = ""
    method_boundary: str = ""
    convergence: str = ""
    applicability: str = ""
    unknown_fields: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _coerce_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        res = dict(data)
        for field_name in (
            "target_quantity",
            "unit",
            "value",
            "uncertainty",
            "state_initial",
            "state_final",
            "transition",
            "j_parity",
            "method_boundary",
            "convergence",
            "applicability",
        ):
            val = res.get(field_name)
            if val is not None and not isinstance(val, str):
                if isinstance(val, (dict, list)):
                    res[field_name] = json.dumps(val, ensure_ascii=False)
                else:
                    res[field_name] = str(val)
        return res


_CARD_TYPE_ALIASES = {
    "STRAT": "STRAT",
    "STRATEGY": "STRAT",
    "STRATEGY_TABLE": "STRAT",
    "WORKFLOW": "STRAT",
    "PROCEDURE": "STRAT",
    "METHOD": "STRAT",
    "METHODOLOGY": "STRAT",
    "APPROACH": "STRAT",
    "MODEL": "MODEL",
    "PHYSICAL_MODEL": "MODEL",
    "CODE": "CODE",
    "FORMULA": "CODE",
    "EQUATION": "CODE",
    "PITFALL": "PITFALL",
    "ERROR": "PITFALL",
    "WARNING": "PITFALL",
    "CONC": "CONC",
    "CONCLUSION": "CONC",
    "RESULT": "CONC",
}


class KnowledgeCardDraft(BaseModel):
    card_type: Literal["STRAT", "MODEL", "CODE", "PITFALL", "CONC"]
    title_zh: str
    title_en: str = ""
    summary_zh: str
    premises_and_conditions: str
    conditions: CardConditions = Field(default_factory=CardConditions)
    element: str | None = None
    z_num: int | None = None
    ion_stage: str | None = None
    isoelectronic: str | None = None
    shell_type: str | None = None
    method: str | None = None
    code_name: str | None = None
    physical_process: str | None = None
    conclusion_flags: int = 0
    structured_details: dict[str, Any] = Field(default_factory=dict)
    formulas_latex: str = ""
    code_snippet: str = ""
    pitfall_warning: str = ""
    keywords: list[str] = Field(default_factory=list)
    supporting_doc_ids: list[str] = Field(default_factory=list)
    source_section: str = ""
    page_num: int | None = None
    confidence: float = 0.0
    search_payload: str = ""
    generalized_conjecture: str | None = None

    @field_validator("keywords", mode="before")
    @classmethod
    def _coerce_keywords(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        res = []
        for item in value:
            if isinstance(item, str) and item.strip():
                res.append(item.strip())
            elif isinstance(item, dict):
                term = str(item.get("term_en") or item.get("term_zh") or item.get("term") or "").strip()
                if term:
                    res.append(term)
        return res

    @field_validator("card_type", mode="before")
    @classmethod
    def _normalize_card_type(cls, value: Any) -> Any:
        raw = str(value or "").strip().upper().replace(" ", "_")
        if raw in _CARD_TYPE_ALIASES:
            return _CARD_TYPE_ALIASES[raw]
        # Web AIs often invent a descriptive type.  Keep the card instead of
        # dropping the whole item because of a non-canonical enum value.
        for token, canonical in (
            ("FORMULA", "CODE"),
            ("EQUATION", "CODE"),
            ("METHOD", "STRAT"),
            ("STRATEG", "STRAT"),
            ("MODEL", "MODEL"),
            ("THEOR", "MODEL"),
            ("PITFALL", "PITFALL"),
            ("ERROR", "PITFALL"),
            ("WARN", "PITFALL"),
            ("CONCLUS", "CONC"),
            ("RESULT", "CONC"),
        ):
            if token in raw:
                return canonical
        return "CONC"

    @field_validator("z_num", mode="before")
    @classmethod
    def _normalize_z_num(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return int(value)
        text = str(value).strip()
        return int(text) if text.isdigit() else None

    @field_validator("formulas_latex", mode="before")
    @classmethod
    def _normalize_formulas(cls, value: Any) -> Any:
        if value is None:
            return ""
        if isinstance(value, (list, tuple)):
            return "\n".join(
                str(item).strip() for item in value if str(item).strip()
            )
        return str(value)

    @model_validator(mode="before")
    @classmethod
    def _merge_root_unknown_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        unknown = data.pop("unknown_fields", None)
        if not unknown:
            return data
        conditions = data.get("conditions")
        if hasattr(conditions, "model_dump"):
            conditions = conditions.model_dump()
        if not isinstance(conditions, dict):
            conditions = {}
        else:
            conditions = dict(conditions)
        existing = conditions.get("unknown_fields") or []
        if isinstance(existing, str):
            existing = [existing]
        merged = list(existing)
        for item in unknown if isinstance(unknown, list) else [unknown]:
            text = str(item).strip()
            if text and text not in merged:
                merged.append(text)
        conditions["unknown_fields"] = merged
        data["conditions"] = conditions
        return data


class ExtractionResult(BaseModel):
    cards: list[KnowledgeCardDraft] = Field(default_factory=list)
    keywords: list[KeywordDraft] = Field(default_factory=list)
    notes: str = ""
    skip: bool = False
    skip_reason: str = ""
    document_title: str = ""
    document_journal: str = ""
    document_year: int | None = None
    document_doi: str = ""


class ConflictVerdict(BaseModel):
    conflict: bool
    severity: float = 0.0
    reason: str = ""
    winner: str = ""
    premise_difference: str = ""
    recommended_action: str = ""


class MasterCardDraft(BaseModel):
    title_zh: str
    summary_zh: str
    isoelectronic: str = ""
    method: str = ""
    unified_commonality: str = ""
    bifurcations: list[dict[str, Any]] = Field(default_factory=list)
    key_findings: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    provenance_chain: list[dict[str, Any]] = Field(default_factory=list)


CARD_SYSTEM_PROMPT = """你是一位顶尖的计算原子物理学家（精通 MCDHF, RCI, RMBPT, CC, CI+MBPT 及 GRASP/FAC/AMBiT/DIRAC 程序）。
现在给你一段从文献 [{doc_id}] 中通过“三点式上下文溯源”提取出的【富上下文证据包】（包含：全局公理与一次性声明、小节全局前提、回溯的模型/表格定义、以及带有 ⟪FOCUS⟫ 标记的核心段落）。

请你完成以下【认知缝合任务】并输出严格的 JSON：
0. 质量控制：若当前片段信息残缺、被截断，或无法提炼出明确的物理结论，直接输出 `{"skip": true}`，不要生成标题带“（证据截断）”或正文为“无法可靠重建具体计算策略”的废卡。
1. 消解所有代词与模型代号：如果结论中提到 "Model B"、"Set II" 或 "this scheme"，你必须结合【回溯定义】和【策略表格】将其替换为具体的物理操作（如：打开了哪一层芯壳层、激发阶数是 SD 还是 SDT、活性空间扩展到哪个 n/l 轨道）。
2. 强制绑定前提条件 (`premises_and_conditions`)：任何结论或计算策略都必须写明其生效的体系特征（如 Z 范围、壳层结构）、理论框架及前置近似，严禁输出脱离前提的孤立断言！
3. 提取数学公式与代码参数：若有哈密顿量、模型势函数（如芯极化势、QED 势），提取标准 LaTeX 并说明各符号物理意义与所做近似（如 NVPA、长波近似）。`formulas_latex` 只能放纯数学表达式，英文解释句必须放入 `summary_zh` 或 `premises_and_conditions`，并过滤 `vuuut1` 等 PDF 私有字体伪影。注意：JSON 内部的所有 LaTeX 反斜杠必须写为双反斜杠（如 \\frac, \\alpha, \\beta 而非单个 \），以防破坏 JSON 语法。
4. 生成中英双语检索载荷 (`search_payload`)：按以下格式拼接，以供 BGE-M3 向量化：
   "[类型:{card_type} | 体系:{ion_stage} ({isoelectronic}, Z={z_num}) | 方法:{method} ({code_name}) | 过程:{physical_process}]
    [前提与边界]: {premises_and_conditions}
    [核心策略与结论(中)]: {summary_zh}
    [Keywords]: {提取出的英文受控专业术语列表}
    [Key Evidence(EN)]: {最关键的 1~2 句英文原句及关键量化指标}"
5. 同时校验文献元数据：若正文/页眉可辨认，请在 JSON 顶层返回 `document_title`、`document_journal`、`document_year`、`document_doi`；无法确认时留空，严禁猜测。
6. 只输出一个 JSON 对象，不要使用 Markdown 代码围栏或额外解释。必须符合给定的 JSON Schema。
7. 适用条件结构化：填写 `conditions` 对象（target_quantity、unit、value、uncertainty、state_initial、state_final、transition、j_parity、method_boundary、convergence、applicability）。未报告的字段填 `unknown`，明确不适用填 `not_applicable`，并把无法判断的字段名放入 `unknown_fields`。不要把未知条件留空后解释成“任意情况均适用”。
8. 广义物理延拓 (`generalized_conjecture`)：若本卡片反映了某种规律、趋势或机理，请遵循原子物理规律做一步合理的广义延拓，严禁胡乱推演；若缺乏明确物理依据或仅为孤立单纯数值，坚决填 null。
"""


MANUAL_MODE_INSTRUCTION = """【程序手册/实操指南增强模式】
当前文献是程序手册、用户指南或实操教程。请把以下内容视为有效知识：
- Worked Examples / Case Study / Sample Run / Test Case / Illustrative calculation；
- In practice / It is advisable / layer by layer / hold ... fixed / spectroscopic orbitals /
  correlation orbitals / weighting / EOL / EAL / zero-order space 等收敛经验；
- rnucleus → rcsfgenerate → rwfnestimate → rmcdhf → rci → rtransition 等工作流与模块职责；
- 终端输入流、批处理脚本、组态生成规则与输入参数。
不得仅因缺少实验误差百分比对比而跳过或判定为“证据截断”。若片段确实只是安装/编译/版权
或仍无法形成明确计算知识，才返回 {"skip": true}。"""


def build_extraction_messages(
    bundle: ContextBundle,
    *,
    doc_metadata: dict[str, Any] | None = None,
    extra_instructions: str = "",
    manual_mode: bool = False,
) -> list[dict[str, str]]:
    metadata = json.dumps(doc_metadata or {}, ensure_ascii=False, indent=2)
    schema = json.dumps(ExtractionResult.model_json_schema(), ensure_ascii=False, indent=2)
    manual = MANUAL_MODE_INSTRUCTION if manual_mode else ""
    user = "\n\n".join(
        part
        for part in (
            f"## 文献元数据\n{metadata}",
            manual,
            bundle.prompt_markdown(),
            "## 输出 JSON Schema\n" + schema,
            "请输出严格 JSON，cards 可以为空数组，但不要输出 JSON 之外的内容。",
            extra_instructions.strip(),
        )
        if part
    )
    system = CARD_SYSTEM_PROMPT.replace("{doc_id}", bundle.doc_id or "")
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def escape_unescaped_quotes(s: str) -> str:
    """Escape unescaped double quotes in a string literal."""
    res = []
    i = 0
    n = len(s)
    while i < n:
        if s[i] == '"':
            bs_count = 0
            j = i - 1
            while j >= 0 and s[j] == '\\':
                bs_count += 1
                j -= 1
            if bs_count % 2 == 0:
                res.append('\\"')
            else:
                res.append('"')
            i += 1
        else:
            res.append(s[i])
            i += 1
    return "".join(res)


def repair_inner_quotes(raw: str) -> str:
    """Repair unescaped double quotes inside single-line JSON string property values."""
    if not raw:
        return raw
    line_pattern = re.compile(r'^(\s*"[a-zA-Z0-9_]+"\s*:\s*")(.*)("(?:\s*,)?\s*)$')
    new_lines = []
    for line in raw.splitlines():
        m = line_pattern.match(line)
        if m:
            prefix, content, suffix = m.group(1), m.group(2), m.group(3)
            new_lines.append(prefix + escape_unescaped_quotes(content) + suffix)
        else:
            new_lines.append(line)
    return "\n".join(new_lines)


def repair_json_text(text: str) -> str:
    """Repair unescaped LaTeX backslashes and invalid escapes in LLM-generated JSON strings."""
    if not text:
        return text
    text = repair_inner_quotes(text)
    result: list[str] = []
    i = 0
    n = len(text)
    in_string = False

    while i < n:
        c = text[i]
        if not in_string:
            if c == '"':
                in_string = True
            result.append(c)
            i += 1
        else:
            if c == '"':
                in_string = False
                result.append(c)
                i += 1
            elif c == '\\':
                if i + 1 >= n:
                    result.append('\\\\')
                    i += 1
                    continue

                nxt = text[i + 1]

                # Standard escapes: \", \\, \/
                if nxt in ('"', '\\', '/'):
                    result.append('\\')
                    result.append(nxt)
                    i += 2
                    continue

                # Unicode escape: \uXXXX
                if nxt == 'u' and i + 5 < n and all(ch in '0123456789abcdefABCDEF' for ch in text[i+2:i+6]):
                    result.append(text[i:i+6])
                    i += 6
                    continue

                # CRLF: \r\n
                if nxt == 'r' and i + 2 < n and text[i+2:i+4] == '\\n':
                    result.append('\\r')
                    i += 2
                    continue

                # Newline: \n followed by non-command char (whitespace, bracket, non-ASCII)
                if nxt == 'n':
                    after_n = text[i + 2] if i + 2 < n else ''
                    if after_n.islower() or (after_n and after_n in '0123456789'):
                        # LaTeX command like \nabla, \nu, \neq, \ne0
                        result.append('\\\\n')
                        i += 2
                        continue
                    else:
                        result.append('\\n')
                        i += 2
                        continue

                # Tab: \t followed by whitespace or quote or comma
                if nxt == 't':
                    after_t = text[i + 2] if i + 2 < n else ''
                    if after_t.isspace() or after_t in ('"', '\\', ','):
                        result.append('\\t')
                        i += 2
                        continue
                    else:
                        # LaTeX like \tau, \theta, \times, \text
                        result.append('\\\\t')
                        i += 2
                        continue

                # All other escapes (\b, \f, \l, \d, \q, \m, \p, etc.):
                # In LaTeX, \b is \beta, \begin, \bar; \f is \frac.
                # In JSON strings from LLMs, they are unescaped LaTeX backslashes.
                result.append('\\\\' + nxt)
                i += 2
            else:
                result.append(c)
                i += 1

    return "".join(result)


def _extract_json_object(text: str) -> Any:
    body = str(text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.+?)```", body, re.S | re.I)
    if fenced:
        body = fenced.group(1).strip()
    start = body.find("{")
    end = body.rfind("}")
    if start < 0 or end <= start:
        raise PromptParseError("响应中没有 JSON 对象")
    body = body[start : end + 1]
    try:
        return json.loads(body)
    except Exception:
        pass
    try:
        return json.loads(repair_json_text(body))
    except ValueError as exc:
        raise PromptParseError(f"JSON 解析失败: {exc}") from exc


def parse_extraction_json(text: str) -> ExtractionResult:
    payload = _extract_json_object(text)
    if isinstance(payload, list):
        payload = {"cards": payload, "keywords": []}
    if not isinstance(payload, dict):
        raise PromptParseError("JSON 根节点必须是对象或数组")
    try:
        return ExtractionResult.model_validate(payload)
    except ValidationError as exc:
        raise PromptParseError(f"卡片 JSON 校验失败: {exc}") from exc


CONFLICT_SYSTEM_PROMPT = """你是计算原子物理领域的学术争议仲裁者。
你会收到两张针对同一物理坐标（离子、方法、过程）但结论极性相反的卡片。
请比较它们的 premises_and_conditions，判断分歧究竟来自：
1. 物理前提不同（Z 范围、活性空间、是否含 Breit/QED、规范选择）；
2. 数值收敛程度不同；
3. 真正的学术争议。
输出严格 JSON：{"conflict": true/false, "severity": 0..1, "reason": "...", "winner": "卡ID或空", "premise_difference": "...", "recommended_action": "..."}
不要输出 JSON 之外的文字。"""


def build_conflict_messages(card_a: dict[str, Any], card_b: dict[str, Any]) -> list[dict[str, str]]:
    user = "卡 A:\n" + json.dumps(card_a, ensure_ascii=False, indent=2)
    user += "\n\n卡 B:\n" + json.dumps(card_b, ensure_ascii=False, indent=2)
    user += "\n\n请输出争议裁决 JSON。"
    return [
        {"role": "system", "content": CONFLICT_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def parse_conflict_json(text: str) -> ConflictVerdict:
    payload = _extract_json_object(text)
    try:
        return ConflictVerdict.model_validate(payload)
    except ValidationError as exc:
        raise PromptParseError(f"争议裁决校验失败: {exc}") from exc


MASTER_SYSTEM_PROMPT = """你是顶尖理论原子物理学家与综述专家。请基于同一文献或体系下的多张 L2 细节卡片及其『广义物理延拓（generalized_conjecture）』，提炼顶层 L1 Master 主卡：
1. 普适共性规律 (unified_commonality)：从各卡片的广义延拓中，归纳支配不同能级/态/核电荷数的深层统一物理机理与标度律；
2. 极性对立与临界相界 (bifurcations)：重点分析截然相反或竞争对抗的结论（如红移vs蓝移、增强vs阻尼），剖析其背后的共同物理根源及在何种临界条件（如临界 Z、壳层开闭、相对论 vs 静电平衡）下发生机制反转；
3. 关键基准发现 (key_findings) 与待解争议 (open_questions)；
4. 在论述中引用卡片编号（如 [CARD-1]）。只输出符合 MasterCardDraft schema 的单一 JSON。"""


def build_master_messages(
    cards: list[dict[str, Any]],
    *,
    isoelectronic: str = "",
    method: str = "",
) -> list[dict[str, str]]:
    payload = {
        "isoelectronic": isoelectronic,
        "method": method,
        "cards": [
            {
                key: card.get(key)
                for key in (
                    "knw_id",
                    "card_type",
                    "title_zh",
                    "summary_zh",
                    "premises_and_conditions",
                    "method",
                    "generalized_conjecture",
                    "consensus_count",
                    "authority_score",
                    "has_conflict",
                )
            }
            for card in cards
        ],
        "schema": MasterCardDraft.model_json_schema(),
    }
    return [
        {"role": "system", "content": MASTER_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=2)},
    ]


def parse_master_json(text: str) -> MasterCardDraft:
    payload = _extract_json_object(text)
    if not isinstance(payload, dict):
        raise PromptParseError("Master 卡片 JSON 根节点必须是对象")
    try:
        return MasterCardDraft.model_validate(payload)
    except ValidationError as exc:
        raise PromptParseError(f"Master 卡片校验失败: {exc}") from exc


CARD_REVISION_SYSTEM_PROMPT = """你是计算原子物理知识库的严谨编辑。
你会收到一张现有知识卡、它的 L3 原始证据块和控制指令。请依据证据修正卡片，
不得编造证据中不存在的事实。修正后的卡片必须保持以下约束：
- 标题、核心结论、前提与边界必须完整；
- `formulas_latex` 只包含纯数学 LaTeX，不能放英文解释句；
- 保留原卡的物理坐标、来源文献、页码等元数据，除非证据明确要求修正；
- 清除 `vuuut1` 等 PDF 私有字体伪影和“证据截断”类废卡内容。
只输出一张卡片 JSON，必须符合给定 JSON Schema，不要输出 Markdown 围栏或解释。"""


def build_card_revision_messages(
    card: dict[str, Any],
    evidence_text: str,
    instruction: str,
) -> list[dict[str, str]]:
    payload = {
        "current_card": card,
        "l3_evidence": str(evidence_text or ""),
        "user_instruction": str(instruction or ""),
        "schema": KnowledgeCardDraft.model_json_schema(),
    }
    return [
        {"role": "system", "content": CARD_REVISION_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=2)},
    ]


def parse_card_revision_json(text: str) -> KnowledgeCardDraft:
    payload = _extract_json_object(text)
    if isinstance(payload, dict) and "card" in payload:
        payload = payload["card"]
    if isinstance(payload, dict) and "cards" in payload:
        cards = payload.get("cards") or []
        if not cards:
            raise PromptParseError("AI 未返回修正后的卡片")
        payload = cards[0]
    try:
        return KnowledgeCardDraft.model_validate(payload)
    except ValidationError as exc:
        raise PromptParseError(f"修正卡片 JSON 校验失败: {exc}") from exc


def build_search_payload_from_draft(card: KnowledgeCardDraft) -> str:
    return "\n".join(
        (
            f"[类型:{card.card_type} | 体系:{card.ion_stage or '—'} "
            f"({card.isoelectronic or '—'}, Z={card.z_num if card.z_num is not None else '—'}) | "
            f"方法:{card.method or '—'} ({card.code_name or '—'}) | "
            f"过程:{card.physical_process or '—'}]",
            f"[前提与边界]: {card.premises_and_conditions}",
            f"[核心策略与结论(中)]: {card.summary_zh}",
            f"[Keywords]: {', '.join(card.keywords)}",
        )
    )
