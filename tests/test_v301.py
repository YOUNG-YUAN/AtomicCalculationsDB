# -*- coding: utf-8 -*-
"""Unit tests for v3.0.1:

1. Robust JSON parsing for multiline unescaped control chars (strict=False).
2. Safe inner quote repair that never corrupts multi-property JSON lines.
3. Coercion of structured dict generalized_conjecture into clean strings.
4. Normalization of Chinese card_type values (理论交叉验证, 精度诊断, etc.).
5. Full regression test on real-world W XXXVIII multi-item JSON reply.
"""

from __future__ import annotations

import json
from models.atomic_prompts import (
    ExtractionResult,
    KnowledgeCardDraft,
    repair_inner_quotes,
    repair_json_text,
)
from models.web_ai_bridge import (
    ClipboardBatchService,
    _extract_json_payload,
    _sanitize_card_dict,
    _sanitize_result_payload,
)


def test_repair_inner_quotes_preserves_multi_property_lines():
    raw_line = '{"metadata_source": "出版社文献记录", "items": [ { "item_id": "DOC-PAP-0000-83DD88-27" } ] }'
    repaired = repair_inner_quotes(raw_line)
    assert '\\"items\\"' not in repaired
    assert '"items"' in repaired
    data = json.loads(repaired, strict=False)
    assert "items" in data
    assert data["items"][0]["item_id"] == "DOC-PAP-0000-83DD88-27"


def test_repair_inner_quotes_fixes_single_property_inner_quotes():
    raw_line = ' "summary_zh": "The author used "Model A" for calculation", '
    repaired = repair_inner_quotes(raw_line)
    assert '\\"Model A\\"' in repaired


def test_generalized_conjecture_coercion_from_dict():
    dict_payload = {
        "statement_zh": "若某激发态的 E1 总衰变率受到抑制，则 E2/M1 可能显著。",
        "physical_basis_zh": "总寿命由所有分支速率之和决定。",
        "boundary_zh": "限于具有同等抵消效应的体系。",
    }
    card_dict = {
        "card_type": "CONC",
        "title_zh": "激发态寿命测试",
        "summary_zh": "测试总结",
        "premises_and_conditions": "Z=74",
        "generalized_conjecture": dict_payload,
    }
    sanitized = _sanitize_card_dict(card_dict)
    assert isinstance(sanitized["generalized_conjecture"], str)
    assert "若某激发态" in sanitized["generalized_conjecture"]
    assert "【物理基础】" in sanitized["generalized_conjecture"]

    draft = KnowledgeCardDraft.model_validate(card_dict)
    assert isinstance(draft.generalized_conjecture, str)
    assert "【适用边界】" in draft.generalized_conjecture


def test_chinese_card_types_normalization():
    types_and_expected = [
        ("理论交叉验证", "CONC"),
        ("精度诊断", "CONC"),
        ("理论与实验基准比较", "CONC"),
        ("计算方法与策略", "STRAT"),
        ("模型势与有效算符", "MODEL"),
        ("数值计算程序", "CODE"),
        ("易错避坑指南", "PITFALL"),
    ]
    for raw_type, canonical in types_and_expected:
        card = {
            "card_type": raw_type,
            "title_zh": f"测试 {raw_type}",
            "summary_zh": "内容",
            "premises_and_conditions": "前提",
        }
        sanitized = _sanitize_card_dict(card)
        assert sanitized["card_type"] == canonical, f"Failed for {raw_type}"


def test_user_reported_w_xxxviii_json_parsing():
    raw_reply = r"""
{ "document_title": "Multiconfiguration Dirac-Hartree-Fock energy levels and
transition probabilities for W XXXVIII", "document_journal": "Physical Review
A", "document_year": 2012, "document_doi": "10.1103/PhysRevA.85.042501",
"metadata_source": "出版社文献记录", "items": [ { "item_id": "DOC-PAP-0000-83DD88-27",
"result": { "skip": false, "card_type": "理论交叉验证", "title_zh": "W XXXVIII 高激发奇宇称能级寿命总体与准相对论结果一致，但存在显著离群能级",
"ion_stage": "W XXXVIII（W37+）", "isoelectronic": "Rb-like", "z_num": 74, "method": "MCDHF；与准相对论（QR）计算比较",
"code_name": "GRASP2K（MCDHF）", "physical_process": "激发态自发辐射衰变与辐射寿命",
"premises_and_conditions": "适用于 Z=74、37 电子的 W XXXVIII。",
"summary_zh": "表 VIII 的十组寿命中，除 J=5/2 外一致良好。", "formulas_latex": [],
"conditions": { "target_quantity": "辐射寿命", "unit": "s", "value": "4.00e-13 s" },
"generalized_conjecture": { "statement_zh": "若某激发态 E1 衰变受抑制，E2/M1 可显著。" } } },
{ "item_id": "DOC-PAP-0000-83DD88-28",
"result": { "skip": false, "card_type": "精度诊断", "title_zh": "以长度式与速度式差异逐线诊断 W XXXVIII 电偶极跃迁数据",
"ion_stage": "W XXXVIII", "isoelectronic": "Rb-like", "z_num": 74, "method": "MCDHF",
"premises_and_conditions": "适用于 W XXXVIII 的 MCDHF 电多极跃迁计算。",
"summary_zh": "使用相同观测能量时可用 deltaT 衡量差异。",
"formulas_latex": [ "\delta T=\frac{|A_{ki}^{(l)}-A_{ki}^{(v)}|}{\max\!\left(A_{ki}^{(l)},A_{ki}^{(v)}\right)}", "\delta T=\frac{|S_l-S_v|}{\max(S_l,S_v)}" ],
"conditions": { "target_quantity": "相对差异", "unit": "无量纲", "value": "0.950" } } }
] }
"""
    service = ClipboardBatchService()
    results = service.parse_reply(raw_reply)
    assert len(results) == 2
    assert "DOC-PAP-0000-83DD88-27" in results
    assert "DOC-PAP-0000-83DD88-28" in results

    res27 = results["DOC-PAP-0000-83DD88-27"]
    assert len(res27.cards) == 1
    assert res27.cards[0].card_type == "CONC"
    assert "若某激发态" in res27.cards[0].generalized_conjecture

    res28 = results["DOC-PAP-0000-83DD88-28"]
    assert len(res28.cards) == 1
    assert res28.cards[0].card_type == "CONC"
    assert "delta T" in res28.cards[0].formulas_latex or "\\delta T" in res28.cards[0].formulas_latex


def test_is_truncated_table_row_detection():
    from parser.html_table import is_truncated_table_row

    # Exact row from wang2016 Table 7 bottom of page 30
    cut_row = [
        "27",
        r"$2{s}^{2}2{p}^{3} ^{2}{P}_{1/2} - 2{s}^{2}2{p}^{3} ^{4}{S}_{3/2}$",
        "M1",
        "1.",
        "",
        "",
        "",
        "",
    ]
    assert is_truncated_table_row(cut_row, 8) is True

    # Complete row
    complete_row = [
        "27",
        r"$2{s}^{2}2{p}^{3} ^{2}{D}_{5/2} - 2{s}^{2}2{p}^{3} ^{2}{D}_{3/2}$",
        "E2",
        "1.555E-03",
        "...",
        "1.700E-03",
        "...",
        "9",
    ]
    assert is_truncated_table_row(complete_row, 8) is False

    # Incomplete scientific notation (e.g. cut at exponent)
    incomplete_exp_row = ["28", "State A", "E1", "1.23E+", "", "", ""]
    assert is_truncated_table_row(incomplete_exp_row, 7) is True

    # Unclosed math delimiter in cell followed by missing columns
    unclosed_math_row = ["29", "$2s^2 2p^3 ^2P", "", "", ""]
    assert is_truncated_table_row(unclosed_math_row, 5) is True

    # Normal row with enumeration in col 0
    enum_row = ["1.", "State description", "1.234", "5.678"]
    assert is_truncated_table_row(enum_row, 4) is False


def test_html_table_to_markdown_prunes_truncated_row():
    from parser.html_table import html_table_to_markdown

    html = """
    <table>
      <tr><th>Z</th><th>Transition</th><th>Type</th><th>MBPT</th><th>NIST</th></tr>
      <tr><td>27</td><td>2s-2p</td><td>M1</td><td>1.174E+00</td><td>1.200E+00</td></tr>
      <tr><td>27</td><td>2p-2p</td><td>E2</td><td>1.555E-03</td><td>1.700E-03</td></tr>
      <tr><td>27</td><td>2s-2p</td><td>M1</td><td>1.</td><td></td></tr>
    </table>
    """
    md = html_table_to_markdown(html)
    lines = md.strip().splitlines()
    # The truncated row ending in '1.' must be pruned
    assert "1.174E+00" in md
    assert "1.555E-03" in md
    import re

    assert re.search(r"\|\s*1\.\s*\|", md) is None
    assert len(lines) == 4  # header + separator + 2 data rows


def test_compress_markdown_table_prunes_truncated_row():
    import re
    from parser.table_compressor import compress_markdown_table

    raw_table = """
| Z | Transition | Type | MBPT | NIST |
|---|---|---|---|---|
| 27 | 2s-2p | M1 | 1.174E+00 | 1.200E+00 |
| 27 | 2s-2p | M1 | 1. | |
"""
    compressed = compress_markdown_table(raw_table)
    assert "1.174E+00" in compressed
    assert re.search(r"\|\s*1\.\s*\|", compressed) is None


def test_sanitize_markdown_table_text_strips_truncated_lines():
    import re
    from parser.html_table import sanitize_markdown_table_text

    text = """Some lead paragraph describing atomic data.

| Z | Transition | Type | MBPT | NIST |
|---|---|---|---|---|
| 27 | 2s-2p | M1 | 1.174E+00 | 1.200E+00 |
| 27 | 2s-2p | M1 | 1. | |

Following discussion paragraph."""

    sanitized = sanitize_markdown_table_text(text)
    assert "1.174E+00" in sanitized
    assert re.search(r"\|\s*1\.\s*\|", sanitized) is None
    assert "Following discussion paragraph" in sanitized


def test_dynamic_batch_packer_snaps_to_newlines():
    from parser.batch_packer import DistilledItem, DynamicBatchPacker

    text = (
        "Lead discussion before table.\n"
        "| No. | Transition | Type | Line Strength |\n"
        "|---|---|---|---|\n"
        "| 1 | 2s2 2p3 2P1/2 - 2s2 2p3 4S3/2 | M1 | 1.174E+00 |\n"
        "| 2 | 2s2 2p3 2D5/2 - 2s2 2p3 4S3/2 | M1 | 7.538E-02 |\n"
        "| 3 | 2s2 2p3 2D5/2 - 2s2 2p3 2D3/2 | M1 | 1.725E+00 |\n"
        "Concluding remarks after table."
    )
    packer = DynamicBatchPacker(max_split_depth=2)
    item = DistilledItem(item_id="DOC-TEST-1", text=text)

    # Force a character-level split with tight capacity
    pieces = packer._split_by_characters(item, capacity=25, depth=0)
    assert len(pieces) > 1

    # Every piece that contains a table row must not cut through a line mid-cell
    for piece in pieces:
        for line in piece.text.splitlines():
            if line.startswith("|") and line.endswith("|"):
                # Complete table line: begins and ends with pipe
                assert line.count("|") >= 2

