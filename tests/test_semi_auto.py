"""Web-AI semi-automatic batch packages: persistence, import and final write."""

from __future__ import annotations

import json
from pathlib import Path

from db.sqlite_manager import SQLiteManager
from models.semi_auto import (
    create_run,
    delete_batch,
    finalize_run,
    find_unfinished_run,
    import_reply,
    load_run,
    parse_documents,
)
from parser.context_bundler import ContextBundle


def _db(tmp_path: Path) -> SQLiteManager:
    return SQLiteManager(
        tmp_path / "workspace",
        embedding_signature="test::dim=4",
    )


def _document(tmp_path: Path, db: SQLiteManager, count: int = 30):
    storage = tmp_path / "workspace" / "storage" / "DOC-PAP-SEMI"
    storage.mkdir(parents=True)
    pdf = storage / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    doc_id = db.upsert_document(
        {"title": "Semi-auto paper", "local_pdf_path": str(pdf)}
    )
    bundles = []
    for index in range(count):
        bundles.append(
            ContextBundle(
                doc_id=doc_id,
                section_path=f"{index + 1}. Method",
                focal_window_markdown=(
                    f"⟪FOCUS⟫ Method {index}: run rmcdhf with active space "
                    f"n={index + 2}. ⟪/FOCUS⟫"
                ),
                focal_sentence=f"Method {index}: run rmcdhf.",
            ).model_dump(mode="json")
        )
    (storage / "bundles.json").write_text(
        json.dumps(bundles, ensure_ascii=False),
        encoding="utf-8",
    )
    return doc_id


def _reply(items: list[dict]) -> str:
    return json.dumps(
        {
            "items": [
                {
                    "item_id": item["item_id"],
                    "result": {
                        "cards": [
                            {
                                "card_type": "STRAT",
                                "title_zh": f"策略 {item['item_id']}",
                                "summary_zh": "按证据包执行。",
                                "premises_and_conditions": "测试前提。",
                            }
                        ]
                    },
                }
                for item in items
            ]
        },
        ensure_ascii=False,
    )


def test_batch_run_splits_by_budget_and_imports_only_at_finalize(tmp_path: Path):
    db = _db(tmp_path)
    try:
        doc_id = _document(tmp_path, db)
        document = db.get_document(doc_id)
        assert document is not None
        manifest = create_run(
            db,
            tmp_path / "workspace",
            [document],
            pipeline=None,
            max_batch_tokens=2_500,
            mode="force",
        )
        assert manifest["item_count"] == 30
        assert len(manifest["batches"]) >= 2
        first = manifest["batches"][0]
        prompt = (
            tmp_path / "workspace" / "semi_auto" / manifest["run_id"]
            / first["prompt_path"]
        ).read_text(encoding="utf-8")
        assert doc_id in prompt
        assert "【文献】" in prompt
        assert first["estimated_tokens"] > 0

        # Import replies: no cards are written before the final step.
        for batch in manifest["batches"]:
            items = json.loads(
                (
                    tmp_path / "workspace" / "semi_auto" / manifest["run_id"]
                    / batch["items_path"]
                ).read_text(encoding="utf-8")
            )
            result = import_reply(
                tmp_path / "workspace", manifest, batch, _reply(items)
            )
            assert result["status"] == "IMPORTED"
            assert result["cards"] == len(items)
            batch = next(
                entry for entry in manifest["batches"]
                if entry["batch_id"] == result["batch_id"]
            )
        assert manifest["status"] == "READY"
        assert db.cards_for_document(doc_id) == []

        old_id = db.upsert_knowledge_card(
            {
                "knw_id": "KNW-CONC-2026-ABCDEF",
                "card_type": "CONC",
                "title_zh": "旧自动卡",
                "summary_zh": "旧结论。",
                "premises_and_conditions": "旧前提。",
                "supporting_doc_ids": [doc_id],
            }
        )
        verified_id = db.upsert_knowledge_card(
            {
                "knw_id": "KNW-CONC-2026-ABCDEE",
                "card_type": "CONC",
                "title_zh": "人工确认卡",
                "summary_zh": "保留。",
                "premises_and_conditions": "前提。",
                "supporting_doc_ids": [doc_id],
                "is_user_verified": 1,
            }
        )

        calls = []

        class FakePipeline:
            qdrant = None

            def _store_draft(self, draft, document, bundle, keywords):
                calls.append((draft.title_zh, document["doc_id"]))
                return True

        result = finalize_run(
            FakePipeline(), db, tmp_path / "workspace", manifest
        )
        assert result["stored"] == 30
        assert len(calls) == 30
        assert db.get_knowledge_card(old_id) is None
        assert db.get_knowledge_card(verified_id) is not None
        assert manifest["status"] == "FINALIZED"
        assert load_run(tmp_path / "workspace", manifest["run_id"])["status"] == "FINALIZED"
    finally:
        db.close()


def test_delete_batch_and_find_unfinished_run(tmp_path: Path):
    db = _db(tmp_path)
    try:
        doc_id = _document(tmp_path, db, count=2)
        manifest = create_run(
            db,
            tmp_path / "workspace",
            [db.get_document(doc_id)],
            pipeline=None,
            max_batch_tokens=6_000,
            mode="force",
        )
        assert find_unfinished_run(tmp_path / "workspace", [doc_id])["run_id"] == manifest["run_id"]
        batch_id = manifest["batches"][0]["batch_id"]
        remaining = delete_batch(
            tmp_path / "workspace", manifest, batch_id
        )
        if remaining is None:
            assert find_unfinished_run(tmp_path / "workspace", [doc_id]) is None
        else:
            assert all(
                item["batch_id"] != batch_id
                for item in remaining["batches"]
            )
    finally:
        db.close()


def test_semi_auto_dialog_loads_persisted_run(qapp, tmp_path: Path):
    from ui.semi_auto_dialog import SemiAutoBatchDialog

    db = _db(tmp_path)
    try:
        doc_id = _document(tmp_path, db, count=4)
        create_run(
            db,
            tmp_path / "workspace",
            [db.get_document(doc_id)],
            pipeline=None,
            max_batch_tokens=2_500,
            mode="force",
        )
        dialog = SemiAutoBatchDialog(
            db,
            tmp_path / "workspace",
            pipeline=None,
            doc_ids=[doc_id],
            mode="force",
        )
        assert "已恢复未完成任务包" in dialog.status_label.text()
        assert dialog.finalize_button.isEnabled() is False
        dialog.close()
    finally:
        db.close()


def test_parse_documents_reports_success_and_failure():
    from pathlib import Path

    class FakePipeline:
        def parse_document(self, doc_id):
            if doc_id == "DOC-BAD":
                return None
            return (None, [object(), object()], Path("bundles.json"))

    result = parse_documents(
        FakePipeline(), ["DOC-A", "DOC-BAD", "DOC-B"]
    )
    assert result["parsed"] == 2
    assert result["bundles"] == 4
    assert result["parsed_doc_ids"] == ["DOC-A", "DOC-B"]
    assert result["failed"][0]["doc_id"] == "DOC-BAD"


def test_parse_reply_accepts_single_card_alias_and_scalar_cleanup():
    from models.web_ai_bridge import ClipboardBatchService

    reply = json.dumps(
        {
            "items": [
                {
                    "item_id": "DOC-1-0",
                    "result": {
                        "skip": False,
                        "card_type": "CONCLUSION",
                        "title_zh": "结论卡",
                        "summary_zh": "结论。",
                        "premises_and_conditions": "前提。",
                        "z_num": "unknown",
                        "formulas_latex": ["E=mc^2", "p=h/λ"],
                        "conditions": {"target_quantity": "energy"},
                        "unknown_fields": ["z_num", "conditions.j_parity"],
                    },
                },
                {
                    "item_id": "DOC-1-1",
                    "result": {
                        "skip": False,
                        "card_type": "METHOD",
                        "title_zh": "方法卡",
                        "summary_zh": "方法。",
                        "premises_and_conditions": "前提。",
                    },
                },
            ]
        },
        ensure_ascii=False,
    )
    results = ClipboardBatchService().parse_reply(reply)
    result = results["DOC-1-0"]
    assert len(result.cards) == 1
    card = result.cards[0]
    assert card.card_type == "CONC"
    assert card.z_num is None
    assert card.formulas_latex == "E=mc^2\np=h/λ"
    assert "z_num" in card.conditions.unknown_fields
    assert "conditions.j_parity" in card.conditions.unknown_fields
    assert results["DOC-1-1"].cards[0].card_type == "STRAT"


def test_regenerate_batches_preserves_imported_and_rechunks_pending(tmp_path: Path):
    from models.semi_auto import regenerate_batches

    db = _db(tmp_path)
    try:
        doc_id = _document(tmp_path, db, count=30)
        document = db.get_document(doc_id)
        assert document is not None

        # 1. Initially create with small budget 2500 -> multiple batches
        manifest = create_run(
            db,
            tmp_path / "workspace",
            [document],
            pipeline=None,
            max_batch_tokens=2_500,
            mode="force",
        )
        initial_batches = manifest["batches"]
        assert len(initial_batches) >= 3

        # 2. Import reply for the first batch
        first_batch = initial_batches[0]
        items_0 = json.loads(
            (
                tmp_path / "workspace" / "semi_auto" / manifest["run_id"]
                / first_batch["items_path"]
            ).read_text(encoding="utf-8")
        )
        res = import_reply(
            tmp_path / "workspace", manifest, first_batch, _reply(items_0)
        )
        assert res["status"] == "IMPORTED"

        # 3. Regenerate with larger budget 20_000 -> fewer batches total
        new_manifest = regenerate_batches(
            db,
            tmp_path / "workspace",
            manifest,
            max_batch_tokens=20_000,
        )
        new_batches = new_manifest["batches"]
        # Batch 01 must be preserved and still IMPORTED!
        assert new_batches[0]["batch_id"] == "BATCH_01"
        assert new_batches[0]["status"] == "IMPORTED"
        assert new_batches[0]["reply_path"] == "batch_01/reply.txt"
        assert (
            tmp_path / "workspace" / "semi_auto" / manifest["run_id"]
            / "batch_01" / "reply.txt"
        ).is_file()

        # The rest of batches should be pending with larger chunk sizes, so fewer total batches
        assert len(new_batches) < len(initial_batches)
        # All 30 items must be preserved across batches without loss or duplication!
        all_item_ids = []
        for b in new_batches:
            all_item_ids.extend(b["item_ids"])
        assert len(all_item_ids) == 30
        assert len(set(all_item_ids)) == 30

        # 4. Now test deleting an imported batch and regenerating again
        delete_batch(tmp_path / "workspace", new_manifest, "BATCH_01")
        assert len(new_manifest["batches"]) == len(new_batches) - 1

        regen_after_delete = regenerate_batches(
            db,
            tmp_path / "workspace",
            new_manifest,
            max_batch_tokens=20_000,
        )
        # All 30 items must be restored and accounted for!
        all_ids_after = []
        for b in regen_after_delete["batches"]:
            all_ids_after.extend(b["item_ids"])
        assert len(all_ids_after) == 30
        assert len(set(all_ids_after)) == 30
    finally:
        db.close()

