import json
import time
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox

from audit.importer import (
    AuditImportError,
    apply_audit_payload,
    preview_audit_payload,
)
from audit.report import export_audit_bundle
from config import AppConfig
from db.card_hash import compute_card_content_hash
from db.hybrid_retriever import HybridRetriever
from db.sqlite_manager import SQLiteManager
from ui.audit_dialogs import AuditImportDialog
from ui.dialogs import DocumentDetailDialog
from ui.views.explore import ExploreView


def _workspace(tmp_path: Path) -> tuple[SQLiteManager, str]:
    db = SQLiteManager(tmp_path / "workspace", embedding_signature="test::dim=4")
    doc = db.upsert_document(
        {
            "title": "Cross-AI audit test",
            "authors": ["A. Author"],
            "journal": "Journal",
            "pub_year": 2024,
            "doi": "10.1234/audit.test",
        }
    )
    return db, doc


def _card(db: SQLiteManager, doc: str, index: int, **extra):
    return db.upsert_knowledge_card(
        {
            "card_level": "L2",
            "card_type": "STRAT",
            "title_zh": f"审计测试卡 {index}",
            "summary_zh": f"原始结论 {index}",
            "premises_and_conditions": "原始前提",
            "supporting_doc_ids": [doc],
            "search_payload": f"audit card alpha {index}",
            "authority_score": 1.2,
            "recency_year": 2024,
            **extra,
        }
    )


def test_schema_v3_audit_columns_and_tables(tmp_path: Path):
    db, _doc = _workspace(tmp_path)
    columns = {
        row["name"]
        for row in db.conn.execute("PRAGMA table_info(knowledge_cards)").fetchall()
    }
    assert {
        "content_hash",
        "audit_status",
        "audit_hidden",
        "audit_delete_suggested",
        "audit_divergence",
        "needs_vector_update",
    } <= columns
    tables = {
        row["name"]
        for row in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {
        "card_model_provenance",
        "card_audit_reviews",
        "card_revisions",
        "document_audit_exports",
        "audit_import_batches",
    } <= tables
    db.close()


def test_provenance_and_content_hash(tmp_path: Path):
    db, doc = _workspace(tmp_path)
    knw_id = _card(db, doc, 1)
    db.set_card_provenance(
        knw_id,
        "fast_llm",
        {
            "node_id": "fast",
            "name": "快速模型",
            "node_type": "OPENAI_COMPATIBLE_API",
            "model": "deepseek-flash",
        },
    )
    card = db.get_knowledge_card(knw_id)
    assert card["provenance"]["fast_llm"]["model"] == "deepseek-flash"
    assert card["content_hash"] == compute_card_content_hash(card)
    old_hash = card["content_hash"]
    db.upsert_knowledge_card({"knw_id": knw_id, "summary_zh": "修改后的结论"})
    new_card = db.get_knowledge_card(knw_id)
    assert new_card["content_hash"] != old_hash
    assert new_card["content_hash"] == compute_card_content_hash(new_card)
    db.close()


def test_export_contains_all_cards_and_split_parts(tmp_path: Path):
    db, doc = _workspace(tmp_path)
    ids = [_card(db, doc, index) for index in range(23)]
    db.mark_card_suggested_delete(
        ids[0], reason="测试软删除", auditor="审计 AI"
    )
    result = export_audit_bundle(
        db,
        tmp_path / "workspace",
        doc,
        max_cards_per_part=10,
        max_chars_per_part=10_000,
    )
    assert result.card_count == 23
    assert result.part_count == 3
    assert (result.export_dir / "report.md").is_file()
    assert (result.export_dir / "cards.jsonl").is_file()
    assert (result.export_dir / "manifest.json").is_file()
    manifest = json.loads(
        (result.export_dir / "manifest.json").read_text(encoding="utf-8")
    )
    assert set(manifest["card_ids"]) == set(ids)
    jsonl = [
        json.loads(line)
        for line in (result.export_dir / "cards.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(jsonl) == 23
    assert any(card["audit_status"] == "SUGGESTED_DELETE" for card in jsonl)
    part_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in result.export_dir.glob("report_part-*.md")
    )
    assert all(knw_id in part_text for knw_id in ids)
    assert len(list(result.export_dir.glob("audit_prompt_part-*.md"))) == 3
    db.close()


def test_audit_export_includes_source_evidence_and_coverage_prompt(tmp_path: Path):
    db, doc = _workspace(tmp_path)
    source = tmp_path / "workspace" / "source.md"
    source.write_text("# Paper\n\n⟪FOCUS⟫ source evidence text ⟪/FOCUS⟫\n", encoding="utf-8")
    db.upsert_document({"doc_id": doc, "local_md_path": "source.md"})
    knw_id = _card(db, doc, 1)
    db.add_evidence(
        knw_id,
        doc,
        "⟪FOCUS⟫ source evidence text ⟪/FOCUS⟫",
        section_title="2. Method",
        page_num=4,
    )
    result = export_audit_bundle(
        db,
        tmp_path / "workspace",
        doc,
        include_source=True,
        include_evidence=True,
        coverage_audit=True,
    )
    assert result.source_included is True
    assert (result.export_dir / "source.md").is_file()
    report = (result.export_dir / "report.md").read_text(encoding="utf-8")
    prompt = (result.export_dir / "audit_prompt.md").read_text(encoding="utf-8")
    manifest = json.loads(
        (result.export_dir / "manifest.json").read_text(encoding="utf-8")
    )
    assert "L3 原始证据" in report
    assert "source evidence text" in report
    assert "覆盖审计模式" in prompt
    assert "source.md" in prompt
    assert manifest["source"]["included"] is True
    assert manifest["source"]["sha256"]
    db.close()


def _payload(
    db: SQLiteManager,
    doc: str,
    report_id: str = "AUD-TEST",
    report_hash: str = "sha256:test",
    reviews=None,
    new_cards=None,
):
    return {
        "schema": "acdb.audit.v1",
        "document_id": doc,
        "report_id": report_id,
        "report_hash": report_hash,
        "batch_id": "part-01",
        "auditor": {"name": "外部审稿 AI", "model": "review-model", "provider": "test"},
        "card_reviews": reviews or [],
        "new_cards": new_cards or [],
    }


def test_apply_modify_delete_add_and_verified_protection(tmp_path: Path):
    db, doc = _workspace(tmp_path)
    modify_id = _card(db, doc, 1)
    delete_id = _card(db, doc, 2)
    verified_id = _card(db, doc, 3, is_user_verified=1)
    modify_card = db.get_knowledge_card(modify_id)
    delete_card = db.get_knowledge_card(delete_id)
    verified_card = db.get_knowledge_card(verified_id)
    payload = _payload(
        db,
        doc,
        reviews=[
            {
                "knw_id": modify_id,
                "content_hash": compute_card_content_hash(modify_card),
                "overall_score": 82,
                "verdict": "基本相符",
                "suggested_action": "modify",
                "revised_card": {"summary_zh": "审计修正后的结论"},
                "issues": [],
            },
            {
                "knw_id": delete_id,
                "content_hash": compute_card_content_hash(delete_card),
                "overall_score": 22,
                "verdict": "基本否定",
                "suggested_action": "delete",
                "delete_reason": "PDF 中没有该结论",
                "issues": [],
            },
            {
                "knw_id": verified_id,
                "content_hash": compute_card_content_hash(verified_card),
                "overall_score": 75,
                "verdict": "基本相符",
                "suggested_action": "modify",
                "revised_card": {"summary_zh": "不应自动覆盖"},
                "issues": [],
            },
        ],
        new_cards=[
            {
                "temp_id": "NEW-01",
                "reason": "报告遗漏的重要结论",
                "card": {
                    "card_type": "MODEL",
                    "title_zh": "新增审计卡",
                    "summary_zh": "新增结论",
                    "premises_and_conditions": "新增前提",
                },
            }
        ],
    )
    preview = preview_audit_payload(db, payload)
    assert preview.stats["modify"] == 2
    assert preview.stats["delete"] == 1
    assert preview.stats["new"] == 1
    result = apply_audit_payload(db, preview)
    assert result.modified == 1
    assert result.deleted == 1
    assert result.added == 1
    assert result.needs_review == 1
    assert db.get_knowledge_card(modify_id)["summary_zh"] == "审计修正后的结论"
    deleted = db.get_knowledge_card(delete_id)
    assert deleted["audit_hidden"] == 1
    assert deleted["audit_status"] == "SUGGESTED_DELETE"
    assert db.get_knowledge_card(verified_id)["summary_zh"] == "原始结论 3"
    assert any(
        review["status"] == "NEEDS_REVIEW"
        for review in db.audit_reviews_for_card(verified_id)
    )
    added = [
        card
        for card in db.cards_for_document(doc)
        if card.get("source_kind") == "AI_AUDIT_ADDED"
    ]
    assert added and added[0]["title_zh"] == "新增审计卡"

    retriever = HybridRetriever(db)
    default_hits, default_total = retriever.search_page("", page=1, page_size=10)
    assert delete_id not in {hit.id for hit in default_hits}
    hidden_hits, hidden_total = retriever.search_page(
        "", page=1, page_size=10, extra_filters={"audit_hidden": 1}
    )
    assert hidden_total == 1
    assert hidden_hits[0].id == delete_id
    assert db.restore_card_from_audit_suggestion(delete_id)
    assert db.get_knowledge_card(delete_id)["audit_hidden"] == 0
    assert default_total >= 3
    db.close()


def test_stale_content_hash_is_not_applied(tmp_path: Path):
    db, doc = _workspace(tmp_path)
    knw_id = _card(db, doc, 1)
    stale = db.get_knowledge_card(knw_id)
    db.upsert_knowledge_card({"knw_id": knw_id, "summary_zh": "用户后来改过"})
    payload = _payload(
        db,
        doc,
        reviews=[
            {
                "knw_id": knw_id,
                "content_hash": compute_card_content_hash(stale),
                "overall_score": 10,
                "verdict": "基本否定",
                "suggested_action": "delete",
                "delete_reason": "旧报告",
                "issues": [],
            }
        ],
    )
    preview = preview_audit_payload(db, payload)
    assert preview.stats["stale"] == 1
    result = apply_audit_payload(db, preview)
    assert result.rejected == 1
    card = db.get_knowledge_card(knw_id)
    assert card["summary_zh"] == "用户后来改过"
    assert card["audit_hidden"] == 0
    db.close()


def test_explore_status_filter_finds_suggested_delete(qapp, tmp_path: Path):
    db, doc = _workspace(tmp_path)
    visible_id = _card(db, doc, 1)
    hidden_id = _card(db, doc, 2)
    db.mark_card_suggested_delete(hidden_id, reason="测试", auditor="AI")
    config = AppConfig({"explore": {"page_size": 10}})
    view = ExploreView(db, HybridRetriever(db), str(tmp_path / "workspace"), config)
    view.show()
    assert _wait_until(qapp, lambda: view.total_results == 1)
    assert visible_id in {card.knw_id for card in view._card_widgets}
    view.status_combo.setCurrentIndex(1)
    assert _wait_until(
        qapp, lambda: hidden_id in {card.knw_id for card in view._card_widgets}
    )
    view.close()
    db.close()


def _wait_until(qapp, predicate, attempts: int = 80) -> bool:
    for _ in range(attempts):
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.03)
    return predicate()


def test_document_detail_audit_buttons_and_import_dialog(qapp, tmp_path: Path):
    db, doc = _workspace(tmp_path)
    knw_id = _card(db, doc, 1)
    detail = DocumentDetailDialog(db, tmp_path / "workspace", doc)
    assert "导出 AI 校对包" in detail.export_audit_button.text()
    assert "导入 AI 校对结果" in detail.import_audit_button.text()
    detail.close()

    card = db.get_knowledge_card(knw_id)
    payload = _payload(
        db,
        doc,
        reviews=[
            {
                "knw_id": knw_id,
                "content_hash": compute_card_content_hash(card),
                "overall_score": 66,
                "verdict": "大致相符",
                "suggested_action": "keep",
                "issues": [],
            }
        ],
    )
    audit_file = tmp_path / "audit.json"
    audit_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    dialog = AuditImportDialog(db, doc)
    dialog.file_edit.setText(str(audit_file))
    dialog._parse()
    assert dialog.preview is not None
    assert dialog.table.rowCount() == 1
    assert dialog.apply_button.isEnabled()
    dialog.close()
    db.close()


def test_report_hash_mismatch_is_rejected(tmp_path: Path):
    db, doc = _workspace(tmp_path)
    knw_id = _card(db, doc, 1)
    result = export_audit_bundle(db, tmp_path / "workspace", doc)
    card = db.get_knowledge_card(knw_id)
    payload = _payload(
        db,
        doc,
        report_id=result.report_id,
        report_hash="sha256:wrong",
        reviews=[
            {
                "knw_id": knw_id,
                "content_hash": compute_card_content_hash(card),
                "overall_score": 80,
                "verdict": "基本相符",
                "suggested_action": "keep",
                "issues": [],
            }
        ],
    )
    try:
        preview_audit_payload(db, payload)
    except AuditImportError as exc:
        assert "report_hash" in str(exc)
    else:
        raise AssertionError("mismatched report hash must be rejected")
    db.close()


def test_multi_ai_divergence_summary(tmp_path: Path):
    db, doc = _workspace(tmp_path)
    knw_id = _card(db, doc, 1)
    card = db.get_knowledge_card(knw_id)
    for auditor, score in (("AI-A", 92), ("AI-B", 28)):
        db.record_audit_review(
            {
                "knw_id": knw_id,
                "doc_id": doc,
                "report_id": "R-" + auditor,
                "report_hash": "H-" + auditor,
                "auditor_name": auditor,
                "overall_score": score,
                "verdict": "完全相符" if score >= 90 else "基本否定",
                "suggested_action": "keep",
                "status": "APPLIED",
            }
        )
    summary = db.refresh_audit_summary(knw_id)
    assert summary["review_count"] == 2
    assert summary["positive"] == 1
    assert summary["negative"] == 1
    assert summary["divergence"] == 1
    assert db.get_knowledge_card(knw_id)["audit_divergence"] == 1
    db.close()


def test_export_opens_file_manager(qapp, tmp_path: Path, monkeypatch):
    db, doc = _workspace(tmp_path)
    _card(db, doc, 1)
    opened: list[str] = []

    class FakeDesktop:
        @staticmethod
        def openUrl(url):
            opened.append(url.toLocalFile())

    class FakeMessageBox:
        Ok = QMessageBox.Ok

        @staticmethod
        def information(*_args, **_kwargs):
            return QMessageBox.Ok

    import ui.dialogs as dialogs_module

    monkeypatch.setattr(dialogs_module, "QDesktopServices", FakeDesktop)
    monkeypatch.setattr(dialogs_module, "QMessageBox", FakeMessageBox)
    detail = dialogs_module.DocumentDetailDialog(db, tmp_path / "workspace", doc)
    detail._export_audit_bundle()
    assert opened
    assert Path(opened[0]).is_dir()
    detail.close()
    db.close()
