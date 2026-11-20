"""Tests for V2.6.0: AGENT_CLI pool nodes and bidirectional MCP workbench/audit."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from config import AppConfig
from db.hybrid_retriever import HybridRetriever
from db.sqlite_manager import SQLiteManager
from models.agent_cli import (
    AgentCLIError,
    build_cli_command,
    run_agent_cli,
)
from models.endpoint_pool import EndpointNode, EndpointPool, default_capacity
from models.gateway import ModelGateway
from models.provenance import node_snapshot
from models.relay_executor import RelayExecutor
from models import semi_auto
from parser.batch_packer import DistilledItem
from parser.context_bundler import ContextBundle
from pipeline import Pipeline
from runtime_bridge import BridgeWorkbench, RuntimeBridgeClient, RuntimeBridgeServer
from sync_mcp.agent_workbench import (
    AgentWorkbenchService,
    WorkbenchPermissionError,
)
from sync_mcp.mcp_server import KnowledgeMCPServer


def _sample_bundle(doc_id: str, idx: int = 0) -> ContextBundle:
    return ContextBundle(
        doc_id=doc_id,
        section_path=f"Section {idx + 1}",
        page_num=idx + 1,
        section_preamble_md="Dirac-Fock calculation setup for heavy ions.",
        focal_sentence=f"We converge the active space at n=6 for case {idx}.",
        focal_window_markdown=(
            f"We converge the active space at n=6 for case {idx}, "
            "using MCDHF+RCI with Breit and QED corrections."
        ),
        detected_channels=["CONC"],
    )


def _seed_doc_with_bundles(db: SQLiteManager, workspace: Path) -> str:
    pdf_dir = workspace / "storage" / "DOC-PAP-2026-ABCDEF"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = pdf_dir / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%fake\n")
    doc_id = db.upsert_document(
        {
            "doc_id": "DOC-PAP-2026-ABCDEF",
            "title": "MCDHF active space convergence benchmark",
            "pub_year": 2026,
            "doc_type": "PAP",
            "download_status": "DOWNLOADED",
            "parse_status": "LIGHT_PARSED",
            "local_pdf_path": str(pdf_path),
        }
    )
    bundles = [_sample_bundle(doc_id, 0), _sample_bundle(doc_id, 1)]
    (pdf_dir / "bundles.json").write_text(
        json.dumps([b.model_dump() for b in bundles], ensure_ascii=False),
        encoding="utf-8",
    )
    (pdf_dir / "parsed.md").write_text("# Section 1\nDirac-Fock setup.\n", encoding="utf-8")
    return doc_id


def test_sqlite_transaction_rollback_and_semi_auto_atomicity(tmp_path: Path):
    db = SQLiteManager(tmp_path)
    try:
        doc_id = _seed_doc_with_bundles(db, tmp_path)
        old_id = db.upsert_knowledge_card(
            {
                "card_level": "L2",
                "card_type": "STRAT",
                "title_zh": "旧卡应当在事务回滚时保留",
                "summary_zh": "旧摘要内容足够长以通过检查",
                "supporting_doc_ids": [doc_id],
                "is_user_verified": 0,
            }
        )
        doc_record = db.get_document(doc_id)
        manifest = semi_auto.create_run(
            db, tmp_path, [doc_record], None, mode="force", max_batch_tokens=20_000
        )
        batch = manifest["batches"][0]
        reply = {
            "items": [
                {
                    "item_id": item_id,
                    "result": {
                        "skip": False,
                        "cards": [
                            {
                                "card_type": "STRAT",
                                "title_zh": f"新提炼卡片 {idx}",
                                "summary_zh": "收敛至 n=6 活性空间并计入 Breit 修正。",
                                "premises_and_conditions": "MCDHF + RCI",
                                "keywords": ["MCDHF"],
                            }
                        ],
                    },
                }
                for idx, item_id in enumerate(batch["item_ids"])
            ]
        }
        semi_auto.import_reply(
            tmp_path,
            manifest,
            batch,
            json.dumps(reply, ensure_ascii=False),
            source_label="Antigravity",
            agent_model="gemini-3.8",
        )

        class BrokenPipeline:
            qdrant = None

            def _store_draft(self, *args, **kwargs):
                raise RuntimeError("simulated disk error")

        with pytest.raises(RuntimeError, match="simulated disk error"):
            semi_auto.finalize_run(BrokenPipeline(), db, tmp_path, manifest)

        # Old unverified card must still exist because SQLite rolled back.
        assert db.get_knowledge_card(old_id) is not None
        assert semi_auto.load_run(tmp_path, manifest["run_id"])["status"] != "FINALIZED"
    finally:
        db.close()


def test_agent_cli_node_roundtrip_command_and_gateway_relay(tmp_path: Path):
    node = EndpointNode(
        node_id="codex-local",
        name="Codex CLI",
        node_type="AGENT_CLI",
        capabilities=["TEXT_LLM"],
        model="gpt-5-codex",
        cli_preset="codex",
        cli_command="codex",
        cli_sandbox="read-only",
        cli_timeout_sec=180,
    )
    restored = EndpointNode.from_dict(node.as_dict())
    assert restored.node_type == "AGENT_CLI"
    assert restored.cli_preset == "codex"
    assert restored.cli_sandbox == "read-only"
    assert default_capacity("AGENT_CLI") == (20_000, 6_000, 8)
    assert node_snapshot(restored)["provider"] == "agent_cli"

    prompt_file = tmp_path / "p.txt"
    output_file = tmp_path / "out.txt"
    cmd, expects_file = build_cli_command(
        restored,
        executable="codex",
        prompt_file=prompt_file,
        output_file=output_file,
        workdir=tmp_path,
    )
    assert expects_file is True
    assert cmd[:4] == ["codex", "exec", "--skip-git-repo-check", "--sandbox"]
    assert "read-only" in cmd
    assert "--output-last-message" in cmd
    assert "-m" in cmd and "gpt-5-codex" in cmd
    assert cmd[-1] == "-"

    custom_node = EndpointNode(
        node_id="custom-cli",
        name="Custom CLI",
        node_type="AGENT_CLI",
        cli_preset="custom",
        cli_command=str(tmp_path / "fake_agent.exe"),
        cli_args="--in {prompt_file} --out {output_file}",
    )
    (tmp_path / "fake_agent.exe").write_text("", encoding="utf-8")

    def fake_runner(argv, **kwargs):
        out_idx = argv.index("--out") + 1
        Path(argv[out_idx]).write_text('{"ok": true, "cards": []}', encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    output = run_agent_cli(
        custom_node,
        "hello",
        workspace=tmp_path,
        runner=fake_runner,
    )
    assert '"ok": true' in output

    cfg = AppConfig(
        {
            "endpoint_pool": {
                "nodes": [custom_node.as_dict()],
                "strategies": [
                    {
                        "strategy_id": "cli-only",
                        "strategy_name": "CLI 优先",
                        "relay_chain": ["custom-cli"],
                    }
                ],
                "role_bindings": {
                    "fast_llm": {"default_strategy_id": "cli-only", "time_slot_rules": []}
                },
            }
        }
    )
    gateway = ModelGateway(cfg, tmp_path)
    assert gateway.fast_client.status().provider == "agent-cli"

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "models.agent_cli.run_agent_cli",
            lambda *a, **kw: '{"skip": false, "cards": [{"card_type": "STRAT", "title_zh": "Codex 策略", "summary_zh": "测试 CLI 调用返回有效卡片"}]}',
        )
        res = gateway.fast_client.chat_json([{"role": "user", "content": "hi"}])
        assert res["cards"][0]["title_zh"] == "Codex 策略"

        relay = RelayExecutor(EndpointPool(cfg))
        items = [DistilledItem(item_id="DOC-1", text="payload")]
        out = relay.run(
            items,
            "fast_llm",
            lambda n, b: SimpleNamespace(
                success_items=list(b.items), failed_items=[], payloads=[n.node_id]
            ),
        )
        assert out.completed == ["custom-cli"]
        assert not out.failed_items

    with pytest.raises(AgentCLIError):
        run_agent_cli(
            EndpointNode(node_id="missing", name="Missing", node_type="AGENT_CLI", cli_command="nonexistent_cli_xyz"),
            "hi",
            workspace=tmp_path,
        )


def test_agent_workbench_semi_auto_and_audit_via_bridge_and_mcp(tmp_path: Path):
    db = SQLiteManager(tmp_path)
    cfg_path = tmp_path / "config.yaml"
    cfg = AppConfig(
        {
            "mcp": {"allow_task_write": True, "allow_direct_db_write": False},
        },
        path=cfg_path,
    )
    cfg.save()
    pipeline = Pipeline(db, tmp_path, config=cfg)
    retriever = HybridRetriever(db)
    changed_events: list[str] = []
    wb = AgentWorkbenchService(
        db,
        tmp_path,
        pipeline=pipeline,
        config=cfg,
        on_data_changed=lambda: changed_events.append("changed"),
        reload_config_from_disk=False,
    )
    bridge = RuntimeBridgeServer(
        retriever, db, tmp_path, port=0, version="2.6.0", workbench=wb
    )
    assert bridge.start()
    try:
        client = RuntimeBridgeClient.discover(tmp_path)
        assert client is not None
        proxy_wb = BridgeWorkbench(client)

        doc_id = _seed_doc_with_bundles(db, tmp_path)
        existing_card_id = db.upsert_knowledge_card(
            {
                "card_level": "L2",
                "card_type": "STRAT",
                "title_zh": "待审校基线卡",
                "summary_zh": "初步摘要描述 n=6 收敛性",
                "supporting_doc_ids": [doc_id],
            }
        )

        tasks = proxy_wb.list_workbench_tasks()
        assert any(d["doc_id"] == doc_id for d in tasks["documents"])

        # 1) Semi-auto batch creation -> prompt -> reply -> permission-gated finalize
        run = proxy_wb.create_semi_auto_batch_run(
            [doc_id], mode="incremental", max_batch_tokens=20_000
        )
        run_id = run["run_id"]
        batch_id = run["batches"][0]["batch_id"]
        prompt_info = proxy_wb.get_semi_auto_batch_prompt(run_id, batch_id)
        assert doc_id in prompt_info["prompt"]

        reply_payload = {
            "items": [
                {
                    "item_id": item_id,
                    "result": {
                        "skip": False,
                        "cards": [
                            {
                                "card_type": "STRAT",
                                "title_zh": f"Agent 提取策略 {idx}",
                                "summary_zh": "使用 MCDHF 扩充活性空间至 n=6 并检查激发态收敛。",
                                "premises_and_conditions": "MCDHF + RCI",
                                "keywords": ["MCDHF"],
                            }
                        ],
                    },
                }
                for idx, item_id in enumerate(prompt_info["item_ids"])
            ]
        }
        submitted = proxy_wb.submit_semi_auto_batch_reply(
            run_id,
            batch_id,
            json.dumps(reply_payload, ensure_ascii=False),
            agent_name="Antigravity",
            agent_model="gemini-3.8",
        )
        assert submitted["ready_to_finalize"] is True

        # Direct DB write is OFF by default -> blocked cleanly
        with pytest.raises(Exception, match="allow_direct_db_write"):
            proxy_wb.finalize_semi_auto_run(run_id, agent_name="Antigravity")

        # Turn ON direct DB write -> finalize succeeds & records MCP_AGENT provenance
        cfg.set("mcp.allow_direct_db_write", True)
        cfg.save()
        finalized = proxy_wb.finalize_semi_auto_run(
            run_id, agent_name="Antigravity", agent_model="gemini-3.8"
        )
        assert finalized["stored"] == 2
        assert len(changed_events) >= 1

        cards = db.list_knowledge_cards(filters={"doc_id": doc_id}, limit=20)
        agent_cards = [c for c in cards if c["title_zh"].startswith("Agent 提取策略")]
        assert len(agent_cards) == 2
        prov = db.provenance_for_card(agent_cards[0]["knw_id"])
        assert prov["fast_llm"]["node_type"] == "MCP_AGENT"
        assert prov["fast_llm"]["model"] == "gemini-3.8"

        # 2) v2.4.0 Cross-model Audit MCP ticket
        ticket = proxy_wb.export_document_audit_ticket(doc_id)
        report_id = ticket["report_id"]
        content = proxy_wb.get_audit_ticket_content(report_id, part=1)
        assert existing_card_id in content["report_markdown"]

        audit_json = json.dumps(
            {
                "schema": "acdb.audit.v1",
                "document_id": doc_id,
                "report_id": report_id,
                "report_hash": ticket["report_hash"],
                "batch_id": content["batch_id"],
                "card_reviews": [
                    {
                        "knw_id": existing_card_id,
                        "overall_score": 82,
                        "verdict": "基本相符",
                        "suggested_action": "modify",
                        "revised_card": {
                            "summary_zh": "经 Antigravity 审计补充：MCDHF n=6 收敛并含 QED 修正。"
                        },
                    }
                ],
                "new_cards": [],
            },
            ensure_ascii=False,
        )
        # First test staging with direct write turned OFF
        cfg.set("mcp.allow_direct_db_write", False)
        cfg.save()
        staged_audit = proxy_wb.submit_audit_ticket_result(
            report_id,
            audit_json,
            auditor_name="Antigravity",
            auditor_model="gemini-3.8",
            apply_to_db=True,
        )
        assert staged_audit["applied_to_db"] is False
        assert Path(staged_audit["staged_file"]).is_file()

        # Now allow direct DB write and apply audit
        cfg.set("mcp.allow_direct_db_write", True)
        cfg.save()
        applied_audit = proxy_wb.submit_audit_ticket_result(
            report_id,
            audit_json,
            auditor_name="Antigravity",
            auditor_model="gemini-3.8",
            apply_to_db=True,
        )
        assert applied_audit["applied_to_db"] is True
        assert applied_audit["apply_result"]["modified"] == 1
        updated_card = db.get_knowledge_card(existing_card_id)
        assert "Antigravity 审计补充" in updated_card["summary_zh"]

        # 3) Disabling mcp.allow_task_write blocks task-write actions
        cfg.set("mcp.allow_task_write", False)
        cfg.save()
        with pytest.raises(WorkbenchPermissionError):
            wb.export_document_audit_ticket(doc_id)

        mcp_server = KnowledgeMCPServer(db, retriever, workbench=wb)
        status = mcp_server.status()
        assert "list_workbench_tasks" in status["tools"]
        assert "submit_audit_ticket_result" in status["tools"]
        assert len(status["tools"]) >= 16
        built = mcp_server.build_server()
        assert built is not None
        import anyio

        tools = anyio.run(built.list_tools)
        tool_names = {t.name for t in tools}
        assert "list_workbench_tasks" in tool_names
        assert "finalize_semi_auto_run" in tool_names
        assert "submit_audit_ticket_result" in tool_names
        res_blocks = anyio.run(built.call_tool, "list_workbench_tasks", {"status": "ALL"})
        content_list = getattr(res_blocks, "content", res_blocks)
        payload = json.loads(content_list[0].text)
        assert payload["ok"] is True
        assert payload["data"]["permissions"]["allow_task_write"] is False
    finally:
        bridge.stop()
        db.close()


def test_v260_ui_agent_cli_dialog_and_sync_mcp_switches(qapp, tmp_path: Path):
    from db.qdrant_manager import QdrantManager
    from sync_mcp.acdb_packager import ACDBPackager
    from sync_mcp.cloud_sync import CloudSyncManager
    from ui.model_pool_dialogs import EndpointNodeDialog
    from ui.views.sync_mcp import SyncMCPView

    dialog = EndpointNodeDialog(workspace=str(tmp_path))
    idx = dialog.node_type.findData("AGENT_CLI")
    assert idx >= 0
    dialog.node_type.setCurrentIndex(idx)
    dialog.node_id.setText("codex-node")
    dialog.name.setText("Codex CLI")
    dialog.model.setText("gpt-5-codex")
    assert dialog.cli_test_button.isVisibleTo(dialog) or not dialog.cli_test_button.isHidden()
    draft = dialog.result_node()
    assert draft.node_type == "AGENT_CLI"
    assert draft.cli_preset == "codex"
    assert draft.cli_command == "codex"
    assert draft.model == "gpt-5-codex"
    dialog.close()

    db = SQLiteManager(tmp_path)
    cfg = AppConfig(path=tmp_path / "config.yaml")
    cfg.save()
    retriever = HybridRetriever(db)
    qdrant = QdrantManager(tmp_path, enabled=False)
    view = SyncMCPView(
        db,
        ACDBPackager(db, tmp_path, qdrant=qdrant),
        CloudSyncManager(db, tmp_path, config=cfg),
        KnowledgeMCPServer(db, retriever),
        qdrant,
        str(tmp_path),
        cfg,
    )
    assert view.mcp_task_write_box.isChecked() is True
    assert view.mcp_direct_write_box.isChecked() is False

    view.mcp_direct_write_box.setChecked(True)
    reloaded = AppConfig.load(tmp_path / "config.yaml", create=False)
    assert reloaded.get("mcp.allow_direct_db_write") is True

    view.mcp_task_write_box.setChecked(False)
    reloaded = AppConfig.load(tmp_path / "config.yaml", create=False)
    assert reloaded.get("mcp.allow_task_write") is False
    assert view.mcp_direct_write_box.isEnabled() is False

    view.close()
    qdrant.close()
    db.close()
