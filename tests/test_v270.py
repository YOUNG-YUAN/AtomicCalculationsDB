"""V2.7.0 Test Suite — Visual Stepper Progress Bar, Cascading Rollback & Deep Synthesis across Web AI, Codex, and Antigravity."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from db.sqlite_manager import SQLiteManager
from models.deep_synthesis import (
    apply_master_synthesis_payload,
    export_master_synthesis_prompt,
)
from models.pipeline_stepper import (
    GlowState,
    StepKey,
    StepStatus,
    compute_document_pipeline_status,
    rollback_pipeline_from_step,
)
from sync_mcp.agent_workbench import AgentWorkbenchService, WorkbenchInputError


@pytest.fixture
def test_db(tmp_path: Path) -> SQLiteManager:
    db_path = tmp_path / "test_v270.db"
    db = SQLiteManager(db_path)
    return db


@pytest.fixture
def sample_doc(test_db: SQLiteManager) -> dict:
    doc = {
        "doc_id": "DOC-PAP-2026-A1B2C3",
        "title": "MCDHF Calculations of Fe XVII Transition Energies",
        "authors": json.dumps(["Zhang San", "Li Si"]),
        "pub_year": 2026,
        "journal": "Phys. Rev. A",
        "doc_type": "PAP",
        "download_status": "DOWNLOADED",
        "parse_status": "LIGHT_PARSED",
    }
    test_db.upsert_document(doc)
    return test_db.get_document(doc["doc_id"])


def test_compute_document_pipeline_status(test_db: SQLiteManager, sample_doc: dict):
    # Case 1: LIGHT_PARSED document with no cards
    status = compute_document_pipeline_status(sample_doc, [])
    assert status.doc_id == sample_doc["doc_id"]
    assert status.steps[0].status == StepStatus.COMPLETED  # 提取文字
    assert status.steps[1].status == StepStatus.COMPLETED  # 提取关键信息
    assert status.steps[2].status == StepStatus.PENDING    # 交付AI速览
    assert status.steps[3].status == StepStatus.PENDING    # 写入数据库
    assert status.steps[4].status == StepStatus.PENDING    # 深度归纳
    assert status.glow == GlowState.NONE

    # Case 2: Document with L2 cards committed (Step 4 completed -> 冒绿光)
    test_db.update_parse_status(sample_doc["doc_id"], "LLM_EXTRACTED")
    l2_card = {
        "knw_id": "KNW-STRAT-2026-A1B2C3",
        "card_level": "L2",
        "card_type": "STRAT",
        "element": "Fe",
        "z_num": 26,
        "ion_stage": "Fe XVII",
        "isoelectronic": "Ne-like",
        "method": "MCDHF",
        "title_zh": "活性空间逐层扩展收敛策略",
        "summary_zh": "通过逐层打开 2s, 2p 芯层轨道并扩展虚轨道至 6g，计算能量收敛稳定。",
        "premises_and_conditions": "Z=26, 类氖结构",
        "supporting_doc_ids": [sample_doc["doc_id"]],
        "search_payload": "Fe XVII MCDHF",
    }
    test_db.upsert_knowledge_card(l2_card, generate_missing=False)

    doc_extracted = test_db.get_document(sample_doc["doc_id"])
    cards = test_db.cards_for_document(sample_doc["doc_id"])
    status2 = compute_document_pipeline_status(doc_extracted, cards)
    assert status2.steps[0].status == StepStatus.COMPLETED
    assert status2.steps[1].status == StepStatus.COMPLETED
    assert status2.steps[2].status == StepStatus.COMPLETED
    assert status2.steps[3].status == StepStatus.COMPLETED  # 写入数据库完成
    assert status2.steps[4].status == StepStatus.PENDING    # 深度归纳未做
    assert status2.glow == GlowState.GREEN  # 冒绿光!

    # Case 3: Step 5 completed with L1 Master card (冒金光)
    master_card = {
        "knw_id": "MASTER-NELIKE-MCDHF",
        "card_level": "L1",
        "card_type": "MASTER",
        "element": "Fe",
        "z_num": 26,
        "ion_stage": "Fe XVII",
        "isoelectronic": "Ne-like",
        "method": "MCDHF",
        "title_zh": "类氖铁离子高阶电子关联与跃迁能量综合指南",
        "summary_zh": "系统总结了 Ne-like Fe XVII 在不同活性空间截断下的相对论关联能演化规律与推荐计算路径。",
        "premises_and_conditions": "综合自文献细节卡",
        "supporting_doc_ids": [sample_doc["doc_id"]],
        "search_payload": "Ne-like Fe XVII Master Guide",
    }
    test_db.upsert_knowledge_card(master_card, generate_missing=False)

    cards_with_master = test_db.cards_for_document(sample_doc["doc_id"])
    status3 = compute_document_pipeline_status(doc_extracted, cards_with_master)
    assert status3.steps[4].status == StepStatus.COMPLETED
    assert status3.glow == GlowState.GOLD  # 冒金光!


def test_cascading_rollback(test_db: SQLiteManager, tmp_path: Path):
    doc_id = "DOC-PAP-2026-F1E2D3"
    test_db.upsert_document({
        "doc_id": doc_id,
        "title": "Rollback Test Document",
        "parse_status": "LLM_EXTRACTED",
        "download_status": "DOWNLOADED",
    })
    # Add L2 card and Master card
    l2_card = {
        "knw_id": "KNW-MODEL-2026-F1E2D3",
        "card_level": "L2",
        "card_type": "MODEL",
        "title_zh": "待回滚细节卡",
        "summary_zh": "细节描述",
        "supporting_doc_ids": [doc_id],
        "search_payload": "test",
    }
    master_card = {
        "knw_id": "MASTER-TEST-ROLL01",
        "card_level": "L1",
        "card_type": "MASTER",
        "title_zh": "待回滚总卡",
        "summary_zh": "总览描述",
        "supporting_doc_ids": [doc_id],
        "search_payload": "test master",
    }
    test_db.upsert_knowledge_card(l2_card, generate_missing=False)
    test_db.upsert_knowledge_card(master_card, generate_missing=False)

    # 1. Rollback from Step 5 (AI_DEEP):
    # Only master card is removed; L2 card remains, status remains LLM_EXTRACTED, glow returns to GREEN
    res5 = rollback_pipeline_from_step(test_db, tmp_path, doc_id, 5)
    assert "MASTER-TEST-ROLL01" in res5["deleted_masters"]
    assert len(res5["deleted_cards"]) == 0
    cards_after_5 = test_db.cards_for_document(doc_id)
    assert len(cards_after_5) == 1
    doc_after_5 = test_db.get_document(doc_id)
    st5 = compute_document_pipeline_status(doc_after_5, cards_after_5)
    assert st5.glow == GlowState.GREEN  # Baseline preserved!

    # 2. Rollback from Step 1 (TXT_EXTRACT):
    # Downstream cards deleted, parse_status becomes PENDING, glow extinguished
    res1 = rollback_pipeline_from_step(test_db, tmp_path, doc_id, 1)
    assert "KNW-MODEL-2026-F1E2D3" in res1["deleted_cards"]
    doc_after_1 = test_db.get_document(doc_id)
    assert doc_after_1["parse_status"] == "PENDING"
    cards_after_1 = test_db.cards_for_document(doc_id)
    st1 = compute_document_pipeline_status(doc_after_1, cards_after_1)
    assert st1.glow == GlowState.NONE  # Glow extinguished!


def test_export_and_apply_master_synthesis(test_db: SQLiteManager):
    doc_id = "DOC-PAP-2026-C4D5E6"
    test_db.upsert_document({
        "doc_id": doc_id,
        "title": "Relativistic Configuration Interaction in highly charged ions",
        "parse_status": "LLM_EXTRACTED",
        "pub_year": 2026,
    })

    # Cannot export prompt if 0 L2 cards
    with pytest.raises(ValueError, match="尚未生成 L2 细节卡片"):
        export_master_synthesis_prompt(test_db, doc_id)

    # Add L2 cards
    card1 = {
        "knw_id": "KNW-STRAT-2026-C4D5E6",
        "card_level": "L2",
        "card_type": "STRAT",
        "element": "W",
        "z_num": 74,
        "ion_stage": "W LXIV",
        "method": "RCI",
        "title_zh": "高电荷态钨的 Breit 相互作用修正",
        "summary_zh": "Breit 相互作用对内壳层激发能量贡献达到 2.3 eV。",
        "premises_and_conditions": "Z=74, 高电荷态",
        "supporting_doc_ids": [doc_id],
        "search_payload": "W LXIV RCI Breit",
    }
    test_db.upsert_knowledge_card(card1, generate_missing=False)

    # Export prompt for Web AI
    exported = export_master_synthesis_prompt(test_db, doc_id)
    assert exported["card_count"] == 1
    assert "深度归纳任务" in exported["prompt"]
    assert "KNW-STRAT-2026-C4D5E6" in exported["prompt"]
    assert "MasterCardDraft" in exported["prompt"] or "title_zh" in exported["prompt"]

    # Web AI returns Master JSON
    web_reply_json = json.dumps({
        "title_zh": "高电荷态高 Z 离子 RCI 关联与相对论修正全局指南",
        "summary_zh": "系统分析了 Breit 相互作用与 QED 效应随核电荷数 Z 的定标规律，给出了推荐的多参考空间方案。",
        "isoelectronic": "Na-like",
        "method": "RCI",
        "key_findings": [
            "对于 Z>70 的离子，频散 Breit 相互作用不可忽略",
            "虚轨道截断至少需要扩展至 7i 层",
        ],
        "open_questions": [
            "双激发核反冲算符在深内壳层的数值误差收敛较慢",
        ],
    }, ensure_ascii=False)

    applied = apply_master_synthesis_payload(test_db, None, doc_id, web_reply_json)
    assert applied["success"] is True
    assert applied["glow"] == "GOLD"
    assert applied["knw_id"].startswith("MASTER-")

    # Verify document now lights up GOLD
    cards = test_db.cards_for_document(doc_id)
    doc = test_db.get_document(doc_id)
    status = compute_document_pipeline_status(doc, cards)
    assert status.glow == GlowState.GOLD


def test_mcp_deep_synthesis_workbench(test_db: SQLiteManager, tmp_path: Path):
    doc_id = "DOC-PAP-2026-B8A7C6"
    test_db.upsert_document({
        "doc_id": doc_id,
        "title": "CI+MBPT Calculations for Divalent Atoms",
        "pub_year": 2026,
        "parse_status": "LLM_EXTRACTED",
    })
    card = {
        "knw_id": "KNW-MODEL-2026-B8A7C6",
        "card_level": "L2",
        "card_type": "MODEL",
        "element": "Ca",
        "z_num": 20,
        "ion_stage": "Ca I",
        "method": "CI+MBPT",
        "title_zh": "二价钙原子的有效哈密顿量构建",
        "summary_zh": "使用微扰论将芯极化势引入组态相互作用矩阵元。",
        "premises_and_conditions": "Ca I",
        "supporting_doc_ids": [doc_id],
        "search_payload": "Ca I CI+MBPT",
    }
    test_db.upsert_knowledge_card(card, generate_missing=False)

    service = AgentWorkbenchService(test_db, tmp_path, reload_config_from_disk=True)

    # 1. Export deep synthesis task
    task = service.export_deep_synthesis_task(doc_id)
    assert task["card_count"] == 1
    assert "CI+MBPT" in task["prompt"]

    # 2. Submit deep synthesis with apply_to_db=False (preview only)
    master_payload = {
        "title_zh": "二价原子 CI+MBPT 方法论综合综述",
        "summary_zh": "全面梳理了价电子关联与芯-价极化效应的处理准则。",
        "method": "CI+MBPT",
        "isoelectronic": "Ca I",
        "key_findings": ["微扰阶数取至二阶足以保证跃迁几率千分之五精度"],
        "open_questions": ["三价电子体系的拓展算法复杂度呈立方增长"],
    }
    preview_res = service.submit_deep_synthesis_result(
        doc_id,
        master_payload,
        apply_to_db=False,
    )
    assert preview_res["applied_to_db"] is False

    # 3. Submit with allow_direct_db_write disabled
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("mcp:\n  allow_direct_db_write: false\n  allow_task_write: true\n")
    no_write_res = service.submit_deep_synthesis_result(
        doc_id,
        master_payload,
        apply_to_db=True,
    )
    assert no_write_res["applied_to_db"] is False
    assert no_write_res["requires_setting"] == "mcp.allow_direct_db_write"

    # 4. Enable allow_direct_db_write and submit
    cfg_file.write_text("mcp:\n  allow_direct_db_write: true\n  allow_task_write: true\n")
    write_res = service.submit_deep_synthesis_result(
        doc_id,
        master_payload,
        apply_to_db=True,
    )
    assert write_res["applied_to_db"] is True
    assert write_res["glow"] == "GOLD"


def test_ui_stepper_widgets(qapp):
    """Smoke test GUI widgets in PySide6 test context."""
    from ui.step_progress_widget import MiniStepProgressWidget, StepActionBar, StepProgressWidget

    stepper = StepProgressWidget()
    stepper.resize(600, 90)

    # Status without cards
    doc = {"doc_id": "TEST-DOC", "parse_status": "LIGHT_PARSED"}
    status = compute_document_pipeline_status(doc, [])
    stepper.set_pipeline_status(status)

    # Click hit test
    cx, cy, r = stepper._node_geometry(1)
    hit = stepper._hit_test_step(cx, cy)
    assert hit == 1

    # Action bar highlight test
    action_bar = StepActionBar()
    action_bar.highlight_next_step(3, GlowState.NONE)

    # Mini stepper
    mini = MiniStepProgressWidget()
    mini.set_pipeline_status(status)
    assert mini.toolTip() != ""
