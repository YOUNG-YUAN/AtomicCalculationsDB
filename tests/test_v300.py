"""Tests for V3.0.0 features:
1. Global preamble upfront condition extraction & injection
2. Deterministic lossless table pre-compression
3. Inductive deep synthesis (generalized_conjecture & provenance_chain)
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from app_core.audit.importer import REVISABLE_FIELDS, _merge_revised_values
from app_core.audit.prompt import build_audit_prompt
from app_core.db.sqlite_manager import SQLiteManager
from app_core.models.atomic_prompts import (
    CardConditions,
    KnowledgeCardDraft,
    MasterCardDraft,
)
from app_core.models.deep_synthesis import (
    apply_master_synthesis_payload,
    export_master_synthesis_prompt,
)
from app_core.parser.context_bundler import ContextBundle, ContextBundler
from app_core.parser.four_channel_radar import RadarHit, RadarResult
from app_core.parser.pdf_to_markdown import ParsedDocument, Section
from app_core.parser.preamble_extractor import extract_document_preamble
from app_core.parser.table_compressor import compress_markdown_table


def test_preamble_extractor_basic():
    doc = ParsedDocument(
        doc_id="DOC-TEST-PREAMBLE",
        title="Relativistic Multiconfiguration Calculations",
        sections=[
            Section(
                title="1. Introduction",
                level=1,
                path="1. Introduction",
                paragraphs=[
                    "Atomic calculations have advanced rapidly in recent years.",
                    "Throughout this paper, atomic units are used unless stated otherwise.",
                    "The nuclear charge distribution is adopted throughout using the Fermi two-parameter model.",
                ],
            ),
            Section(
                title="2. Theoretical Method",
                level=1,
                path="2. Theoretical Method",
                paragraphs=[
                    "In all calculations, the Breit interaction and QED corrections are included to first order.",
                    "Random unrelated sentence describing experimental setup.",
                ],
            ),
            Section(
                title="References",
                level=1,
                path="References",
                paragraphs=[
                    "Throughout this paper, atomic units are used in cited works.",
                ],
            ),
        ],
    )

    preambles = extract_document_preamble(doc)
    assert len(preambles) >= 2
    assert any("atomic units" in p for p in preambles)
    assert any("Fermi two-parameter" in p or "Breit" in p for p in preambles)
    # References section should be skipped
    assert not any("cited works" in p for p in preambles)


def test_table_compressor_unit_lifting_and_index_dropping():
    raw_table = """
    | No. | State | Energy | Transition Rate |
    | --- | ----- | ------ | --------------- |
    | 1   | 2s2p  | 12.5 eV| 1.4e8 s^-1      |
    | 2   | 2s3p  | 18.2 eV| 3.1e8 s^-1      |
    | 3   | 2p3d  | 25.0 eV| 8.9e8 s^-1      |
    
    * Note: All energies are relative to the ground state.
    """
    compressed = compress_markdown_table(raw_table)

    # 1. Index column "No." dropped
    assert "No." not in compressed
    # 2. Units lifted into headers
    assert "Energy [ev]" in compressed
    assert "Transition Rate [s^-1]" in compressed
    # 3. Units stripped from data cells
    assert "| 12.5 |" in compressed
    assert "| 18.2 |" in compressed
    # 4. Footnote preserved
    assert "* Note: All energies are relative to the ground state." in compressed


def test_context_bundle_preamble_and_table_compression():
    doc = ParsedDocument(
        doc_id="DOC-001",
        title="Test Ingestion",
        sections=[
            Section(
                title="1. Method",
                level=1,
                path="1. Method",
                paragraphs=[
                    "Throughout this work, atomic units are adopted throughout.",
                    "The calculation adopts Model A for core-valence correlation.",
                    "We observe a pronounced shift in fine-structure splittings.",
                ],
            )
        ],
    )

    raw_table = (
        "| Index | Level | Energy |\n"
        "| --- | --- | --- |\n"
        "| 1 | 3s | 5.2 eV |\n"
        "| 2 | 3p | 7.8 eV |\n"
    )

    hit = RadarHit(
        channel="TABLE",
        text=raw_table,
        section_path="1. Method",
        paragraph_index=2,
        sentence="We observe a pronounced shift in fine-structure splittings.",
        table_markdown=raw_table,
    )
    radar = RadarResult(hits=[hit])

    bundler = ContextBundler()
    bundles = bundler.build(doc, radar, doc_id="DOC-001")
    assert len(bundles) == 1
    bundle = bundles[0]

    assert len(bundle.document_preamble) >= 1
    assert "atomic units" in bundle.document_preamble[0].lower()
    assert "Index" not in bundle.table_markdown
    assert "Energy [ev]" in bundle.table_markdown

    prompt_md = bundle.prompt_markdown()
    assert "## 全局公理与一次性声明 (Document Preamble)" in prompt_md
    assert "atomic units" in prompt_md.lower()
    assert "## 表格数据 (压缩脱水版)" in prompt_md


def test_knowledge_card_draft_generalized_conjecture():
    data = {
        "card_type": "STRAT",
        "title_zh": "高电荷态激发态关联收敛策略",
        "summary_zh": "通过逐层开放芯壳层，显著提高了高 Z 离子的能级计算精度。",
        "premises_and_conditions": "类锂等电子序列 Z=20-50",
        "generalized_conjecture": "当 Z > 30 时，核库仑场主导使单粒子势占优，轨道弛豫效应随 Z^4 迅速弱化。",
    }
    draft = KnowledgeCardDraft(**data)
    assert draft.generalized_conjecture is not None
    assert "Z > 30" in draft.generalized_conjecture


def test_master_card_draft_and_provenance_chain():
    master_json = {
        "title_zh": "类锂高 Z 离子高精度计算综合 Master 卡",
        "summary_zh": "全面总结了多组态狄拉克-福克方法与相对论多体微扰理论在类锂离子的收敛规律。",
        "unified_commonality": "随着核电荷数增加，电子关联能占总能量比例反比下降，相对论单粒子势居于绝对主导。",
        "bifurcations": [
            {
                "topic": "Breit 修正影响",
                "finding_a": "低 Z 离子 Breit 修正占跃迁能不足 0.1%",
                "finding_b": "超高 Z (Z>70) 离子由于磁相互作用剧烈，Breit 贡献高达 5%",
                "phase_boundary": "临界转折发生在 Z ≈ 45 附近",
            }
        ],
        "key_findings": ["RMBPT 在单价电子体系极其高效", "MCDHF 对激发态具有极高自洽性"],
        "open_questions": ["QED 高阶顶角校正在极高 Z 仍有争议"],
    }
    master = MasterCardDraft(**master_json)
    assert "随着核电荷数增加" in master.unified_commonality
    assert len(master.bifurcations) == 1
    assert "Z ≈ 45" in master.bifurcations[0]["phase_boundary"]

    # Mock database
    mock_db = MagicMock()
    mock_db.get_document.return_value = {
        "doc_id": "DOC-TEST-99",
        "title": "High-Z Lithium-like Calculations",
        "pub_year": 2026,
    }
    mock_db.cards_for_document.return_value = [
        {
            "knw_id": "KNW-001",
            "doc_id": "DOC-TEST-99",
            "title_zh": "低 Z 关联计算",
            "card_level": "L2",
            "card_type": "STRAT",
            "method": "MCDHF",
            "source_section": "Sec. 3.1",
            "page_num": 3,
            "locator": {"section_path": "Sec. 3.1", "page_num": 3, "table_caption": "Table 1"},
        },
        {
            "knw_id": "KNW-002",
            "doc_id": "DOC-TEST-99",
            "title_zh": "高 Z 相对论效应",
            "card_level": "L2",
            "card_type": "MODEL",
            "method": "RMBPT",
            "source_section": "Sec. 4.2",
            "page_num": 7,
            "locator": {"section_path": "Sec. 4.2", "page_num": 7, "table_caption": ""},
        },
    ]

    res_export = export_master_synthesis_prompt(mock_db, "DOC-TEST-99")
    assert "DOC-TEST-99" in res_export["doc_id"]
    assert "DOC-TEST-99" in res_export["prompt"]
    assert "KNW-001" in res_export["prompt"]
    assert "普适共性规律" in res_export["prompt"]
    assert "极性对立与临界相界" in res_export["prompt"]

    res_apply = apply_master_synthesis_payload(mock_db, None, "DOC-TEST-99", master_json)
    knw_id = res_apply["knw_id"]
    assert knw_id.startswith("MASTER-")

    # Verify upsert call contains provenance_chain in structured_details_json
    assert mock_db.upsert_knowledge_card.called
    card_saved = mock_db.upsert_knowledge_card.call_args[0][0]
    assert card_saved["card_level"] == "L1"
    assert card_saved["card_type"] == "MASTER"

    details = json.loads(card_saved["structured_details_json"])
    assert "provenance_chain" in details
    assert len(details["provenance_chain"]) == 2
    assert details["provenance_chain"][0]["knw_id"] == "KNW-001"
    assert details["provenance_chain"][0]["source_section"] == "Sec. 3.1"
    assert details["provenance_chain"][0]["table_caption"] == "Table 1"
    assert details["provenance_chain"][1]["knw_id"] == "KNW-002"
    assert details["unified_commonality"] == master_json["unified_commonality"]


def test_audit_enrich_generalized_conjecture(tmp_path):
    # 1. Verify build_audit_prompt mentions generalized_conjecture and principle feasibility
    prompt = build_audit_prompt(
        document_id="DOC-AUDIT-01",
        report_id="AUD-01",
        report_hash="sha256:123456",
        batch_id="part-01",
        card_count=1,
    )
    assert "generalized_conjecture" in prompt
    assert "原理上是否可行" in prompt
    assert "遵循原子物理规律" in prompt
    assert "generalized_conjecture" in REVISABLE_FIELDS

    # 2. Verify _merge_revised_values merges generalized_conjecture into structured_details_json
    before_card = {
        "knw_id": "KNW-TEST-AUDIT-01",
        "card_level": "L2",
        "title_zh": "旧卡片无延拓",
        "summary_zh": "原初提取总结",
        "premises_and_conditions": "Z=10",
        "structured": {"conditions": {"target_quantity": "Energy"}},
        "supporting_doc_ids_list": ["DOC-AUDIT-01"],
    }
    revised = {
        "generalized_conjecture": "当 Z>30 时单粒子势主导，轨道弛豫效应随 Z^4 迅速弱化",
    }
    values, _ = _merge_revised_values(before_card, revised)
    assert values["generalized_conjecture"] == "当 Z>30 时单粒子势主导，轨道弛豫效应随 Z^4 迅速弱化"
    assert values["structured_details_json"]["generalized_conjecture"] == values["generalized_conjecture"]

    # Verify: if card already had an infeasible conjecture, audit can correct it
    before_with_conjecture = {
        "knw_id": "KNW-TEST-AUDIT-02",
        "card_level": "L2",
        "title_zh": "存在原理缺陷延拓的卡片",
        "summary_zh": "原初提取总结",
        "generalized_conjecture": "原版有原理缺陷的推论",
        "supporting_doc_ids_list": ["DOC-AUDIT-01"],
    }
    audit_correction = {
        "generalized_conjecture": "校对修正后的合乎原理的延拓",
    }
    values_corrected, _ = _merge_revised_values(before_with_conjecture, audit_correction)
    assert values_corrected["generalized_conjecture"] == "校对修正后的合乎原理的延拓"

    # 3. Verify SQLite round-trip
    db = SQLiteManager(tmp_path)
    db.upsert_document({"doc_id": "DOC-PAP-2026-000001", "title": "Test Audit Doc", "pub_year": 2026})
    knw_id = db.upsert_knowledge_card({
        "knw_id": "KNW-STRAT-2026-000001",
        "card_level": "L2",
        "card_type": "STRAT",
        "title_zh": "测试老卡片",
        "summary_zh": "旧结论",
        "supporting_doc_ids": ["DOC-PAP-2026-000001"],
    })

    card = db.get_knowledge_card(knw_id)
    assert card["generalized_conjecture"] is None

    # Apply audit enrichment
    db.upsert_knowledge_card({
        "knw_id": knw_id,
        "title_zh": "测试老卡片",
        "summary_zh": "旧结论",
        "generalized_conjecture": "校对阶段补充的广义延拓推论",
    })

    card_updated = db.get_knowledge_card(knw_id)
    assert card_updated["generalized_conjecture"] == "校对阶段补充的广义延拓推论"

    # Also verify cards_for_document returns it directly
    cards = db.cards_for_document("DOC-PAP-2026-000001")
    assert len(cards) == 1
    assert cards[0]["generalized_conjecture"] == "校对阶段补充的广义延拓推论"


def test_document_audit_dialog_and_step4_button(tmp_path, qapp):
    from app_core.ui.audit_dialogs import DocumentAuditDialog
    from app_core.ui.task_scope_dialog import TaskScopeDialog

    db = SQLiteManager(tmp_path)
    db.upsert_document({"doc_id": "DOC-PAP-2026-000001", "title": "Test Audit Doc", "pub_year": 2026})
    db.upsert_knowledge_card({
        "knw_id": "KNW-STRAT-2026-000001",
        "card_level": "L2",
        "card_type": "STRAT",
        "title_zh": "测试卡片",
        "summary_zh": "结论",
        "generalized_conjecture": "测试延拓",
        "supporting_doc_ids": ["DOC-PAP-2026-000001"],
    })

    # 1. Test TaskScopeDialog step4_audit
    scope = TaskScopeDialog(db=db, task_type="step4_audit", selected_doc_ids=["DOC-PAP-2026-000001"])
    assert "AI 校对" in scope.window_title
    assert "第 ④ 步" in scope.header_title

    # 2. Test DocumentAuditDialog instantiation and meta summary
    audit_dlg = DocumentAuditDialog(db, tmp_path, ["DOC-PAP-2026-000001"])
    assert "AI 校对工作台" in audit_dlg.windowTitle()
    assert "已具广义延拓：1 张" in audit_dlg.meta_summary_label.text()
    assert audit_dlg.copy_audit_button is not None
    assert audit_dlg.import_audit_button is not None
    audit_dlg.close()


def test_model_pool_v3_categories_and_audit_strategy():
    from app_core.models.endpoint_pool import (
        ROLE_KEYS,
        ROLE_LABELS,
        EndpointPool,
        ModelStrategy,
    )

    assert "audit_llm" in ROLE_KEYS
    assert ROLE_LABELS["fast_llm"] == "AI 快速提取"
    assert ROLE_LABELS["slow_llm"] == "AI 深度归纳"
    assert ROLE_LABELS["audit_llm"] == "AI 校对核查"
    assert ROLE_LABELS["vlm"] == "AI 图像理解"

    fake_config = {
        "endpoint_pool": {
            "strategies": [
                {
                    "strategy_id": "s1",
                    "strategy_name": "快速提取 API策略",
                    "relay_chain": [],
                },
                {
                    "strategy_id": "s2",
                    "strategy_name": "深度归纳 API策略",
                    "relay_chain": [],
                },
                {
                    "strategy_id": "s3",
                    "strategy_name": "图像理解 API策略",
                    "relay_chain": [],
                },
            ],
            "nodes": [
                {"node_id": "n1", "name": "快速提取 API"},
                {"node_id": "n2", "name": "深度归纳 API"},
                {"node_id": "n3", "name": "图像理解 API"},
            ],
            "role_bindings": {},
        }
    }
    pool = EndpointPool(fake_config)
    # Check migrations
    assert pool.strategies["s1"].strategy_name == "AI 快速提取策略"
    assert pool.strategies["s2"].strategy_name == "AI 深度归纳策略"
    assert pool.strategies["s3"].strategy_name == "AI 图像理解策略"
    assert pool.nodes["n1"].name == "AI 快速提取"
    assert pool.nodes["n2"].name == "AI 深度归纳"
    assert pool.nodes["n3"].name == "AI 图像理解"

    # AI 校对核查策略 auto-created
    assert any("校对核查" in s.strategy_name for s in pool.strategies.values())
    assert pool.bindings["audit_llm"].default_strategy_id != ""


def test_strategy_toggle_switch_widget(qapp):
    from app_core.models.endpoint_pool import EndpointNode, ModelStrategy
    from app_core.ui.controls import StrategyToggleSwitch, SwitchButton
    from app_core.ui.model_pool_dialogs import StrategyDialog

    # 1. Switch widget test
    switch = StrategyToggleSwitch(checked=True)
    assert switch.isChecked()
    assert "当前节点已开启" in switch.label.text()
    switch.setChecked(False)
    assert not switch.isChecked()
    assert "当前节点已关闭" in switch.label.text()

    # 2. Strategy Dialog test for vision strategy
    vlm_strategy = ModelStrategy(
        strategy_id="legacy-vlm-strategy",
        strategy_name="AI 图像理解策略",
        relay_chain=[],
        enabled=True,
    )
    node = EndpointNode(node_id="v1", name="Vision Node", capabilities=["VISION_VLM"])
    dlg = StrategyDialog([node], strategy=vlm_strategy)
    assert dlg.is_vision_strategy
    assert dlg.toggle_switch is not None
    assert dlg.toggle_switch.isChecked()

    dlg.toggle_switch.setChecked(False)
    saved_strategy = dlg.result_strategy()
    assert saved_strategy.enabled is False
    dlg.close()


def test_task_scope_dialog_mode_switch(tmp_path, qapp):
    from app_core.ui.task_scope_dialog import TaskScopeDialog

    db = SQLiteManager(tmp_path)
    db.upsert_document({"doc_id": "DOC-PAP-2026-000001", "title": "Test Doc", "pub_year": 2026})

    dialog = TaskScopeDialog(db, task_type="step3_api", selected_doc_ids=["DOC-PAP-2026-000001"])
    assert dialog.get_execution_mode() == "auto"
    assert "依照策略自动速览" in dialog.confirm_button.text()

    # Switch to semi-auto
    dialog.radio_mode_semi.setChecked(True)
    assert dialog.get_execution_mode() == "semi_auto"
    assert "打开半自动工作台" in dialog.confirm_button.text()
    assert "零 API Token 成本" in dialog.status_label.text()
    dialog.close()


def test_library_toolbar_buttons_v3(tmp_path, qapp):
    from app_core.ui.views.library import LibraryView

    db = SQLiteManager(tmp_path)
    lib = LibraryView(db=db, fetcher=MagicMock(), workspace=str(tmp_path))

    assert lib.step3_api_button.text() == "⚡ ③ AI 快速理解"
    assert lib.step4_audit_button.text() == "🔍 ④ AI 校对核查"
    assert lib.step5_deep_button.text() == "✨ ⑤ AI 深度归纳"
    assert lib.step5_deep_button.objectName() == "Primary"
    assert not hasattr(lib, "step3_semi_button")


def test_semi_auto_batch_single_doc_constraint(tmp_path, qapp):
    from app_core.models.semi_auto import create_run
    from app_core.ui.semi_auto_dialog import PasteReplyDialog

    # 1. Test PasteReplyDialog modal UI
    paste_dlg = PasteReplyDialog(2)
    assert "粘贴提问包 2 的网页 AI 回复" in paste_dlg.windowTitle()
    paste_dlg.text_edit.setPlainText('{"items": []}')
    assert paste_dlg.get_text() == '{"items": []}'
    paste_dlg.close()

    # 2. Setup mock documents with bundles.json
    db = SQLiteManager(tmp_path)
    doc1_dir = tmp_path / "raw_papers" / "DOC-PAP-2026-000001"
    doc2_dir = tmp_path / "raw_papers" / "DOC-PAP-2026-000002"
    doc1_dir.mkdir(parents=True)
    doc2_dir.mkdir(parents=True)

    pdf1 = doc1_dir / "paper.pdf"
    pdf2 = doc2_dir / "paper.pdf"
    pdf1.write_bytes(b"%PDF-test")
    pdf2.write_bytes(b"%PDF-test")

    bundle_payload1 = [{
        "doc_id": "DOC-PAP-2026-000001",
        "section_path": "1. Test",
        "focal_sentence": "Energy calculation for Fe XVII.",
        "focal_window_markdown": "Atomic energy calculation in eV.",
    }]
    bundle_payload2 = [{
        "doc_id": "DOC-PAP-2026-000002",
        "section_path": "1. Test",
        "focal_sentence": "Energy calculation for Fe XVII.",
        "focal_window_markdown": "Atomic energy calculation in eV.",
    }]
    (doc1_dir / "bundles.json").write_text(json.dumps(bundle_payload1), encoding="utf-8")
    (doc2_dir / "bundles.json").write_text(json.dumps(bundle_payload2), encoding="utf-8")

    d1 = {"doc_id": "DOC-PAP-2026-000001", "title": "Paper 1", "local_pdf_path": str(pdf1)}
    d2 = {"doc_id": "DOC-PAP-2026-000002", "title": "Paper 2", "local_pdf_path": str(pdf2)}
    db.upsert_document(d1)
    db.upsert_document(d2)

    manifest = create_run(db, tmp_path, [d1, d2], pipeline=MagicMock(), mode="force")
    batches = manifest["batches"]
    assert len(batches) == 2

    # CRITICAL CHECK: Every batch must strictly contain items from only 1 document!
    for b in batches:
        assert len(b["doc_ids"]) == 1
    assert batches[0]["doc_ids"] == ["DOC-PAP-2026-000001"]
    assert batches[1]["doc_ids"] == ["DOC-PAP-2026-000002"]
    assert "album_pdf_path" in batches[0]


def test_semi_auto_dialog_doc_table_and_scroll_v3(tmp_path, qapp):
    from app_core.ui.semi_auto_dialog import SemiAutoBatchDialog

    db = SQLiteManager(tmp_path)
    doc_ids = []
    for i in range(15):
        did = f"DOC-PAP-2026-{i+1:06d}"
        doc_ids.append(did)
        db.upsert_document({
            "doc_id": did,
            "title": f"Atomic Structure Paper {i+1}",
            "doc_type": "paper",
            "pub_year": 2020 + (i % 5),
            "journal": "Phys. Rev. A",
            "authors": json.dumps([f"Author {i+1}A", f"Author {i+1}B"]),
            "input_tokens": 10000 + i * 500,
            "output_tokens": 2000 + i * 100,
        })

    dlg = SemiAutoBatchDialog(db, tmp_path, pipeline=MagicMock(), doc_ids=doc_ids)

    # 1. Document table checks
    assert hasattr(dlg, "doc_table")
    assert dlg.doc_table.columnCount() == 7
    headers = [dlg.doc_table.horizontalHeaderItem(c).text() for c in range(7)]
    assert headers == ["DOC-ID", "标题", "类型", "年份", "期刊", "作者", "Token 消耗"]
    assert dlg.doc_table.rowCount() == 15
    assert not dlg.doc_table.verticalHeader().isHidden()
    # Height capped for 10 visible rows
    assert dlg.doc_table.height() <= 320

    # First row content verification
    assert dlg.doc_table.item(0, 0).text() == "DOC-PAP-2026-000001"
    assert dlg.doc_table.item(0, 1).text() == "Atomic Structure Paper 1"
    assert dlg.doc_table.item(0, 2).text() == "论文"
    assert dlg.doc_table.item(0, 3).text() == "2020"
    assert dlg.doc_table.item(0, 4).text() == "Phys. Rev. A"
    assert "Author 1A" in dlg.doc_table.item(0, 5).text()
    assert "入: 10,000 / 出: 2,000" in dlg.doc_table.item(0, 6).text()

    # 2. Batch section checks
    assert hasattr(dlg, "batch_box")
    assert hasattr(dlg, "scroll")
    from PySide6.QtCore import Qt
    assert dlg.scroll.verticalScrollBarPolicy() == Qt.ScrollBarAsNeeded

    # 3. Batch row badge check
    dummy_batch = {
        "slot": 1,
        "batch_id": "BATCH_01",
        "doc_ids": ["DOC-PAP-2026-000001"],
        "item_ids": ["item_1", "item_2"],
        "estimated_tokens": 45000,
        "status": "PENDING",
    }
    card = dlg._batch_row(dummy_batch, finalized=False)
    # Check that row number badge exists
    labels = card.findChildren(type(dlg.status_label))
    assert any("行号 #01" in l.text() for l in labels)

    dlg.close()


def test_encrypted_model_pool_storage_v3(tmp_path):
    from app_core.config import AppConfig
    from app_core.models.endpoint_pool import EndpointNode, EndpointPool
    from app_core.models.security import decrypt_text, encrypt_text

    # 1. Roundtrip test for text encryption
    secret = "sk-super-secret-key-xyz-777"
    enc = encrypt_text(secret)
    assert secret not in enc
    assert decrypt_text(enc) == secret

    # 2. EndpointPool integration test with workspace
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    cfg_path = workspace / "config.yaml"
    cfg = AppConfig(data={}, path=cfg_path)
    pool = EndpointPool(cfg)

    # Insert a custom node with sensitive credentials
    node = EndpointNode(
        node_id="sec-node-1",
        name="Secure API Node",
        api_key=secret,
        base_url="https://api.example.com/v1",
    )
    pool.upsert_node(node)
    pool.save()
    cfg.save()

    # Verify dedicated encrypted file exists
    enc_file = workspace / "model_pool.enc"
    assert enc_file.is_file()
    enc_bytes = enc_file.read_bytes()
    assert secret.encode("utf-8") not in enc_bytes

    # Verify config.yaml does not leak the key
    cfg_text = cfg_path.read_text(encoding="utf-8")
    assert secret not in cfg_text
    assert "model_pool.enc" in cfg_text

    # Reload from disk into fresh pool
    cfg2 = AppConfig(path=cfg_path)
    pool2 = EndpointPool(cfg2)
    loaded_node = pool2.nodes.get("sec-node-1")
    assert loaded_node is not None
    assert loaded_node.api_key == secret


def test_document_detail_dialog_force_button_v3(tmp_path, qapp):
    db = SQLiteManager(tmp_path / "workspace", embedding_signature="test::dim=4")
    doc_id = db.upsert_document({
        "title": "Relativistic self-consistent fields test",
        "pub_year": 1961,
        "authors": ["I. P. Grant"],
    })

    from app_core.ui.dialogs import DocumentDetailDialog
    dlg = DocumentDetailDialog(db, tmp_path / "workspace", doc_id)

    # Verify force_button and incremental_button exist and match step buttons
    assert hasattr(dlg, "force_button")
    assert hasattr(dlg, "incremental_button")
    assert dlg.force_button is dlg.action_bar.btn_step1
    assert dlg.incremental_button is dlg.action_bar.btn_step3

    # Test _handle_step_action(1) with pipeline/reprocess_manager
    mock_pipeline = MagicMock()
    mock_reprocess = MagicMock()
    mock_reprocess.estimate_force_tokens.return_value = 1000
    dlg.pipeline = mock_pipeline
    dlg.reprocess_manager = mock_reprocess

    from PySide6.QtWidgets import QMessageBox
    with MagicMock() as mock_box:
        # User clicks No on confirm
        from unittest.mock import patch
        with patch.object(QMessageBox, "question", return_value=QMessageBox.No):
            dlg._handle_step_action(1)
            # Should safely return without starting worker
            assert dlg._worker is None

        # User clicks Yes on confirm
        with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
            with patch("app_core.ui.dialogs.ReprocessWorker") as mock_worker_cls:
                mock_worker = MagicMock()
                mock_worker_cls.return_value = mock_worker
                dlg._handle_step_action(1)
                assert dlg.action_bar.isEnabled() is False
                assert dlg.force_button.isEnabled() is False
                assert dlg.incremental_button.isEnabled() is False
                mock_worker.start.assert_called_once()

    # Test _on_reprocess_done and _on_reprocess_failed handlers safe execution
    dlg._on_reprocess_done({"extracted": 0, "cancelled": True})
    assert dlg.force_button.isEnabled() is True
    assert dlg.incremental_button.isEnabled() is True
    assert dlg.action_bar.isEnabled() is True

    dlg._on_reprocess_failed("Test error")
    assert dlg.force_button.isEnabled() is True
    assert dlg.incremental_button.isEnabled() is True
    assert dlg.action_bar.isEnabled() is True


def test_pipeline_stepper_step3_model_failure_status_v3():
    from app_core.models.pipeline_stepper import compute_document_pipeline_status, StepStatus

    # Case 1: Model extraction failure on bundles -> Step 3 should be FAILED, not Step 2
    doc_fail_model = {
        "doc_id": "DOC-PAP-0000-833BEB",
        "parse_status": "FAILED",
        "parse_error_code": "",
        "error_log": "未生成知识卡：55 个候选片段模型提取失败，其余片段未产生有效内容。",
        "local_md_path": "storage/DOC-PAP-0000-833BEB/parsed.md",
    }
    status = compute_document_pipeline_status(doc_fail_model, [])
    assert status.steps[0].status == StepStatus.COMPLETED
    assert status.steps[1].status == StepStatus.COMPLETED
    assert status.steps[2].status == StepStatus.FAILED
    assert status.steps[2].name == "AI 快速提取"
    assert "模型提取失败" in status.steps[2].error_message
    assert status.current_step == 3

    # Case 2: Text/OCR extraction failure -> Step 1 should be FAILED
    doc_fail_txt = {
        "doc_id": "DOC-PAP-0000-833BEB",
        "parse_status": "FAILED",
        "parse_error_code": "NEEDS_OCR",
        "error_log": "需要文本层",
    }
    status_txt = compute_document_pipeline_status(doc_fail_txt, [])
    assert status_txt.steps[0].status == StepStatus.FAILED

    # Case 3: Step names check
    assert status.steps[2].name == "AI 快速提取"
    assert status.steps[4].name == "AI 深度归纳"


def test_document_detail_dialog_strategy_bar_v3(tmp_path, qapp):
    from unittest.mock import patch, MagicMock
    db = SQLiteManager(tmp_path / "workspace", embedding_signature="test::dim=4")
    doc_id = db.upsert_document({
        "title": "Strategy Bar Test Doc",
        "pub_year": 2024,
        "authors": ["Test Author"],
        "parse_status": "LIGHT_PARSED",
    })

    from app_core.ui.dialogs import DocumentDetailDialog
    dlg = DocumentDetailDialog(db, tmp_path / "workspace", doc_id)

    # 1. Strategy bar presence & controls
    assert hasattr(dlg, "strategy_frame")
    assert hasattr(dlg, "fast_mode_combo")
    assert hasattr(dlg, "deep_mode_combo")
    assert dlg.fast_mode_combo.count() >= 2
    assert dlg.deep_mode_combo.count() == 2

    # Check labels and values
    assert dlg.fast_mode_combo.itemData(0) == "auto"
    assert dlg.fast_mode_combo.itemData(1) == "manual"
    assert dlg.deep_mode_combo.itemData(0) == "auto"
    assert dlg.deep_mode_combo.itemData(1) == "manual"

    # 2. Step 3 Action in manual mode -> opens semi-auto
    dlg.fast_mode_combo.setCurrentIndex(1)  # manual
    with MagicMock() as mock_open:
        dlg._open_semi_auto = mock_open
        dlg._handle_step_action(3)
        mock_open.assert_called_once()

    # 3. Step 5 Action in manual mode -> opens DeepSynthesisDialog on Tab 0
    # Add a mock L2 card so step 5 check passes
    db.upsert_knowledge_card({
        "supporting_doc_ids": [doc_id],
        "title_zh": "Test Card",
        "card_level": "L2",
    })
    dlg.deep_mode_combo.setCurrentIndex(1)  # manual
    with patch("app_core.ui.dialogs.DeepSynthesisDialog") as mock_synth_cls:
        mock_synth = MagicMock()
        mock_synth_cls.return_value = mock_synth
        dlg._handle_step_action(5)
        mock_synth.tabs.setCurrentIndex.assert_called_with(0)
        mock_synth.exec.assert_called_once()

    # Step 5 Action in auto mode -> opens DeepSynthesisDialog on Tab 1
    dlg.deep_mode_combo.setCurrentIndex(0)  # auto
    with patch("app_core.ui.dialogs.DeepSynthesisDialog") as mock_synth_cls:
        mock_synth = MagicMock()
        mock_synth_cls.return_value = mock_synth
        dlg._handle_step_action(5)
        mock_synth.tabs.setCurrentIndex.assert_called_with(1)
        mock_synth.exec.assert_called_once()


def test_web_ai_bridge_parse_reply_robust_sanitization_v3():
    from app_core.models.web_ai_bridge import ClipboardBatchService

    web_reply_json = """
    {
      "items": [
        {
          "item_id": "ITEM_TEST_01",
          "result": {
            "skip": false,
            "document_title": "Test Atomic Paper",
            "document_year": "",
            "document_journal": "PRL",
            "title": "测试 Dirac 轨道变分方程",
            "card_type": "methodology",
            "z_num": "unknown",
            "premises_and_conditions": "单电子原子前提",
            "summary_zh": "测试结论摘要内容",
            "formulas_latex": ["\\\\alpha = 1", "\\\\beta = 2"],
            "generalized_conjecture": "测试延拓物理规律"
          }
        },
        {
          "item_id": "ITEM_TEST_02",
          "result": {
            "skip": true
          }
        }
      ]
    }
    """
    service = ClipboardBatchService()
    results = service.parse_reply(web_reply_json)
    assert len(results) == 2
    assert "ITEM_TEST_01" in results
    assert "ITEM_TEST_02" in results

    item1 = results["ITEM_TEST_01"]
    assert item1.skip is False
    assert item1.document_year is None
    assert len(item1.cards) == 1
    card = item1.cards[0]
    assert card.title_zh == "测试 Dirac 轨道变分方程"
    assert card.card_type == "STRAT"
    assert card.z_num is None
    assert "\\alpha = 1\n\\beta = 2" in card.formulas_latex
    assert card.structured_details.get("generalized_conjecture") == "测试延拓物理规律"

    item2 = results["ITEM_TEST_02"]
    assert item2.skip is True


def test_clipboard_batch_service_latex_unescaped_backslashes():
    from app_core.models.web_ai_bridge import ClipboardBatchService
    raw_reply_with_unescaped_latex = r"""
    {
      "items": [
        {
          "item_id": "ITEM_LATEX_01",
          "result": {
            "skip": false,
            "document_title": "Relativistic self-consistent fields",
            "cards": [
              {
                "card_type": "CODE",
                "title_zh": "双电子相互作用矩阵元",
                "summary_zh": "直接积分与交换积分定义",
                "premises_and_conditions": "多电子原子体系\n[前提与边界]: 条件1",
                "formulas_latex": "J(A,B)=\langle A,B|g|A,B\rangle,\qquad h=\mathrm{i}c\boldsymbol{\alpha}\cdot\nabla-\beta c^{2}-\frac{N}{r}."
              }
            ]
          }
        }
      ]
    }
    """
    service = ClipboardBatchService()
    results = service.parse_reply(raw_reply_with_unescaped_latex)
    assert len(results) == 1
    assert "ITEM_LATEX_01" in results
    card = results["ITEM_LATEX_01"].cards[0]
    assert r"\langle" in card.formulas_latex
    assert r"\frac" in card.formulas_latex
    assert r"\boldsymbol" in card.formulas_latex
    assert r"\beta" in card.formulas_latex
    assert r"\nabla" in card.formulas_latex


def test_clipboard_batch_service_unescaped_inner_quotes():
    from app_core.models.web_ai_bridge import ClipboardBatchService
    raw_reply_with_quotes = r"""
    {
      "items": [
        {
          "item_id": "ITEM_QUOTE_01",
          "result": {
            "skip": false,
            "document_title": "Transition probability calculations",
            "cards": [
              {
                "card_type": "CODE",
                "title_zh": "用逆重叠平方根构造正交归一轨道",
                "summary_zh": "选择C=S^(-1/2)X，可将原非正交轨道线性变换为正交归一轨道。",
                "premises_and_conditions": "采用非正交单电子轨道二次量子化",
                "search_payload": "[类型:CODE]\n[Key Evidence(EN)]: Original fragment: "where X is a unitary matrix, the new orbitals constitute an orthonormal set". Eq. (3): C=S^(-1/2)X."
              }
            ]
          }
        }
      ]
    }
    """
    service = ClipboardBatchService()
    results = service.parse_reply(raw_reply_with_quotes)
    assert len(results) == 1
    assert "ITEM_QUOTE_01" in results
    assert len(results["ITEM_QUOTE_01"].cards) == 1


def test_batch_prompt_packet_prefix_and_misalignment_guard(tmp_path):
    import pytest
    from app_core.models.semi_auto import (
        create_run,
        read_prompt,
        import_reply,
        BatchMisalignedError,
    )
    from app_core.parser.context_bundler import ContextBundle

    db = SQLiteManager(tmp_path)
    doc_id = "DOC-PAP-2026-000099"
    storage = tmp_path / "storage" / doc_id
    storage.mkdir(parents=True)
    pdf = storage / "paper.pdf"
    pdf.write_bytes(b"%PDF dummy")
    db.upsert_document({"doc_id": doc_id, "title": "Misalign Test", "local_pdf_path": str(pdf)})

    bundles = [
        ContextBundle(doc_id=doc_id, section_path=f"Sec {i}", focal_sentence=f"Sentence {i}")
        for i in range(4)
    ]
    (storage / "bundles.json").write_text(json.dumps([b.model_dump() for b in bundles], ensure_ascii=False), encoding="utf-8")

    manifest = create_run(db, tmp_path, [db.get_document(doc_id)], pipeline=MagicMock(), max_batch_tokens=3000)
    assert len(manifest["batches"]) >= 1

    # 1. Verify prompt packet starts with explicit batch header
    first_b = manifest["batches"][0]
    prompt_text = read_prompt(tmp_path, manifest, first_b)
    assert prompt_text.startswith("【提问包】BATCH_01 · 槽位 01")

    # 2. Test misalignment guard
    # Construct a synthetic multi-batch manifest to verify BatchMisalignedError
    dummy_manifest = {
        "run_id": manifest["run_id"],
        "batches": [
            {"batch_id": "BATCH_01", "slot": 1, "item_ids": ["ITEM_A1", "ITEM_A2"], "prompt_path": first_b["prompt_path"], "items_path": first_b["items_path"]},
            {"batch_id": "BATCH_02", "slot": 2, "item_ids": ["ITEM_B1", "ITEM_B2"], "prompt_path": first_b["prompt_path"], "items_path": first_b["items_path"]},
        ],
    }
    reply_for_b2 = json.dumps({
        "items": [
            {
                "item_id": "ITEM_B1",
                "result": {"skip": False, "cards": [{"card_type": "CODE", "title_zh": "测试", "summary_zh": "摘要", "premises_and_conditions": "前提"}]}
            }
        ]
    })
    # Pasting into BATCH_01 should raise BatchMisalignedError with target BATCH_02
    with pytest.raises(BatchMisalignedError) as exc_info:
        import_reply(tmp_path, dummy_manifest, dummy_manifest["batches"][0], reply_for_b2)
    assert exc_info.value.target_batch["batch_id"] == "BATCH_02"
    assert "【提问包错位拦截】" in str(exc_info.value)


def test_pipeline_stepper_bundles_exist_marks_step3_failed(tmp_path):
    from app_core.models.pipeline_stepper import compute_document_pipeline_status, StepStatus

    storage = tmp_path / "storage" / "DOC-PAP-STEPPER"
    storage.mkdir(parents=True)
    pdf = storage / "paper.pdf"
    pdf.write_bytes(b"%PDF dummy")
    bundles = storage / "bundles.json"
    bundles.write_text('[{"bundle": 1}]', encoding="utf-8")

    doc = {
        "doc_id": "DOC-PAP-STEPPER",
        "title": "Stepper Test",
        "local_pdf_path": str(pdf),
        "parse_status": "FAILED",
        "parse_error_code": "",
        "error_log": "未生成知识卡：所有候选片段均被判定为证据截断或无有效原子物理内容。",
    }
    status = compute_document_pipeline_status(doc, [])
    # Step 1 and Step 2 must be completed because bundles exist
    assert status.steps[0].status == StepStatus.COMPLETED
    assert status.steps[1].status == StepStatus.COMPLETED
    # Step 3 must be marked FAILED
    assert status.steps[2].status == StepStatus.FAILED


def test_card_conditions_and_keywords_structure_coercion():
    from app_core.models.atomic_prompts import CardConditions, KnowledgeCardDraft, ExtractionResult
    from app_core.models.web_ai_bridge import ClipboardBatchService

    # 1. Conditions containing dict and numeric value
    cond = CardConditions.model_validate({
        "target_quantity": "energy levels",
        "unit": {"energy": "cm^-1", "ratio": "%"},
        "value": {"level_1": 126606, "level_2": 128147},
        "uncertainty": 0.05,
    })
    assert isinstance(cond.unit, str)
    assert "cm^-1" in cond.unit
    assert isinstance(cond.value, str)
    assert "126606" in cond.value
    assert cond.uncertainty == "0.05"

    # 2. Keywords containing dict objects (term_en) and single-card extraction result
    raw_json = json.dumps({
        "items": [
            {
                "item_id": "TEST_ITEM_COERCE",
                "result": {
                    "card_type": "STRAT",
                    "title": "测试策略",
                    "summary_zh": "测试摘要",
                    "premises_and_conditions": "测试前提",
                    "keywords": [{"term_en": "barium"}, "excitation"],
                    "conditions": {
                        "value": {"series": 14}
                    }
                }
            }
        ]
    })
    service = ClipboardBatchService()
    results = service.parse_reply(raw_json)
    assert "TEST_ITEM_COERCE" in results
    res = results["TEST_ITEM_COERCE"]
    assert len(res.cards) == 1
    card = res.cards[0]
    assert card.keywords == ["barium", "excitation"]
    assert "14" in card.conditions.value











