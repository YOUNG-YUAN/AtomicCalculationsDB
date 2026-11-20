"""AtomicCalculationDB V2.0 entry point.

Normal launch starts the Qt GUI + night worker + inbox watcher.
``--mcp`` starts a stdio MCP server without Qt; ``--mcp-sse`` starts SSE.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import threading
import traceback
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QMessageBox

from config import AppConfig, ConfigError
from crawler.manual_inbox_watcher import ManualInboxWatcher
from crawler.waterfall_fetcher import WaterfallFetcher
from db.hybrid_retriever import HybridRetriever
from db.qdrant_manager import QdrantManager
from db.sqlite_manager import SQLiteManager
from logging_setup import setup_logging
from models.endpoint_pool import EndpointPool
from models.gateway import ModelGateway
from pipeline import Pipeline
from runtime_bridge import (
    BridgeDB,
    BridgeRetriever,
    BridgeWorkbench,
    RuntimeBridgeClient,
    RuntimeBridgeServer,
)
from scheduler.night_worker import NightWorker
from sync_mcp.acdb_packager import ACDBPackager
from sync_mcp.agent_workbench import AgentWorkbenchService
from sync_mcp.cloud_sync import CloudSyncManager
from sync_mcp.mcp_server import KnowledgeMCPServer
from ui.icon_assets import app_icon
from ui.main_window import MainWindow, apply_theme
from updater.updater import read_version

log = logging.getLogger(__name__)


def _prepare_app_id() -> None:
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "AtomicCalculationDB.App.V2"
            )
        except Exception:
            pass


def _load_config(workspace: Path) -> AppConfig:
    path = workspace / "config.yaml"
    try:
        return AppConfig.load(path, create=True)
    except ConfigError as exc:
        log.error("Config load failed, backing up and recreating: %s", exc)
        if path.exists():
            try:
                shutil.copy2(path, path.with_suffix(".yaml.broken"))
            except OSError:
                pass
        config = AppConfig(path=path)
        config.save()
        return config


def _embedding_signature(config: AppConfig) -> tuple[str, int]:
    """Resolve the locked vector signature from the unified endpoint pool."""
    pool = EndpointPool(config)
    node = pool.node_for_role("embedding")
    if node is None:
        return "BAAI/bge-m3::dim=1024", 1024
    model = str(node.model or "BAAI/bge-m3")
    dimension = int(node.model_dim or 1024)
    return f"{model}::dim={dimension}", dimension


def _init_services(base: Path, config: AppConfig) -> dict:
    workspace = base / "workspace"
    for relative in (
        "storage",
        "inbox_manual",
        "morning_reports",
        "exports",
        "models",
        "logs",
        "browser_profile",
        "sync_snapshots",
        "sync_incoming",
        "vector_db",
    ):
        (workspace / relative).mkdir(parents=True, exist_ok=True)

    signature, dimension = _embedding_signature(config)
    db = SQLiteManager(workspace, embedding_signature=signature)
    if db.embedding_signature_mismatch:
        log.error(
            "Embedding signature changed: stored=%s configured=%s",
            db.embedding_signature(),
            signature,
        )
    qdrant = QdrantManager(
        workspace,
        collection=str(config.get("vectors.collection", "knowledge_cards")),
        dimension=dimension,
        embedding_signature=signature,
        enabled=bool(config.get("vectors.enabled", True)),
    )
    gateway = ModelGateway(config, workspace, db=db)
    retriever = HybridRetriever(
        db,
        qdrant=qdrant,
        embedder=gateway.embedding,
        reranker=gateway.reranker,
    )
    fetcher = WaterfallFetcher(db, workspace, config=config)
    pipeline = Pipeline(
        db,
        workspace,
        config=config,
        gateway=gateway,
        qdrant=qdrant,
        retriever=retriever,
        fetcher=fetcher,
    )
    worker = NightWorker(
        db, workspace_dir=str(workspace), config=config
    )
    pipeline.register_handlers(worker)
    workbench = AgentWorkbenchService(
        db,
        workspace,
        pipeline=pipeline,
        config=config,
        reload_config_from_disk=True,
    )
    mcp_server = KnowledgeMCPServer(db, retriever, workbench=workbench)
    packager = ACDBPackager(db, workspace, qdrant=qdrant)

    def vectorize_card(card: dict) -> list[float] | None:
        if not getattr(qdrant, "available", False):
            return None
        if not getattr(gateway.embedding, "available", False):
            return None
        text = str(card.get("search_payload") or card.get("summary_zh") or "")
        return gateway.embedding.embed_query(text) if text else None

    sync_manager = CloudSyncManager(
        db,
        workspace,
        config=config,
        vector_dir=workspace / "vector_db",
        packager=packager,
        vectorizer=vectorize_card,
    )
    watcher = ManualInboxWatcher(db, workspace)
    from parser.card_quality import purge_invalid_cards, sanitize_existing_cards
    from parser.context_bundler import bundle_hash_from_text

    purge_invalid_cards(db, qdrant)
    sanitize_existing_cards(db, qdrant)
    db.backfill_bundle_hashes(bundle_hash_from_text)
    return {
        "workspace": workspace,
        "db": db,
        "qdrant": qdrant,
        "gateway": gateway,
        "retriever": retriever,
        "fetcher": fetcher,
        "pipeline": pipeline,
        "worker": worker,
        "workbench": workbench,
        "mcp_server": mcp_server,
        "packager": packager,
        "sync_manager": sync_manager,
        "watcher": watcher,
    }


def _parse_cli(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="AtomicCalculationDB", add_help=True)
    parser.add_argument("--base", default=None, help="Release root directory")
    parser.add_argument("--workspace", default=None, help="Workspace directory")
    parser.add_argument("--mcp", action="store_true", help="Start MCP stdio server")
    parser.add_argument("--mcp-sse", dest="mcp_sse", action="store_true", help="Start MCP SSE server")
    parser.add_argument("--host", default=None, help="Host for MCP server")
    parser.add_argument("--port", type=int, default=None, help="Port for MCP server")

    subparsers = parser.add_subparsers(dest="subcommand", help="Subcommand to execute")
    agent_parser = subparsers.add_parser("agent", help="AI Agent CLI operations")
    agent_sub = agent_parser.add_subparsers(dest="agent_action", help="Agent action")

    # agent export <doc_id> [--out-dir <dir>] [--force]
    export_p = agent_sub.add_parser("export", help="Export decoupled task bundle for a document")
    export_p.add_argument("doc_id", help="Document ID (e.g. DOC-PAP-0000-E284F0)")
    export_p.add_argument("--out-dir", dest="out_dir", default=None, help="Output directory")
    export_p.add_argument("--force", action="store_true", help="Force re-parsing if needed")

    # agent submit <doc_id> [result_file] [--json <json_str>] [--agent-name <name>] [--agent-model <model>]
    submit_p = agent_sub.add_parser("submit", help="Submit extraction results to DB")
    submit_p.add_argument("doc_id", help="Document ID")
    submit_p.add_argument(
        "result_file",
        nargs="?",
        default=None,
        help="Path to result.json (defaults to workspace/agent_tasks/<doc_id>/result.json)",
    )
    submit_p.add_argument("--json", dest="result_json", default=None, help="Direct JSON string of results")
    submit_p.add_argument("--agent-name", default="agent", help="Agent identifier name")
    submit_p.add_argument("--agent-model", default="", help="Model name used by agent")

    # agent status <doc_id>
    status_p = agent_sub.add_parser("status", help="Get agent task status for a document")
    status_p.add_argument("doc_id", help="Document ID")

    # agent verify <doc_id> [result_file]
    verify_p = agent_sub.add_parser("verify", help="Verify result.json against items.json and schema")
    verify_p.add_argument("doc_id", help="Document ID")
    verify_p.add_argument(
        "result_file",
        nargs="?",
        default=None,
        help="Path to result.json (defaults to workspace/agent_tasks/<doc_id>/result.json)",
    )

    # agent execute <doc_id> [--interval <sec>] [--model <model>] [--agent-name <name>] [--force] [--no-submit]
    execute_p = agent_sub.add_parser(
        "execute",
        help="Execute extraction with pause & rate-limit polling resilience (every 5-10m)",
    )
    execute_p.add_argument("doc_id", help="Document ID")
    execute_p.add_argument(
        "--interval",
        dest="poll_interval",
        type=int,
        default=300,
        help="Polling interval in seconds when hitting rate limit / timeout (default: 300s = 5m)",
    )
    execute_p.add_argument("--model", dest="agent_model", default="", help="LLM model name")
    execute_p.add_argument("--agent-name", default="agent", help="Agent identifier")
    execute_p.add_argument("--force", action="store_true", help="Force re-processing already completed items")
    execute_p.add_argument(
        "--no-submit", dest="no_submit", action="store_true", help="Do not auto-submit to DB on completion"
    )

    args, _unknown = parser.parse_known_args(argv[1:])
    for idx, token in enumerate(argv):
        if token == "--base" and idx + 1 < len(argv) and getattr(args, "base", None) is None:
            setattr(args, "base", argv[idx + 1])
        elif token == "--workspace" and idx + 1 < len(argv) and getattr(args, "workspace", None) is None:
            setattr(args, "workspace", argv[idx + 1])
    return args


def _close_services(services: dict) -> None:
    for key in ("watcher", "worker", "qdrant", "db"):
        obj = services.get(key)
        if obj is not None and hasattr(obj, "close"):
            try:
                obj.close()
            except Exception:
                pass


def _run_agent_cli(base: Path, config: AppConfig, args: argparse.Namespace) -> int:
    action = getattr(args, "agent_action", None)
    if not action:
        print("Error: Missing agent action (export, submit, status). Run with --help for details.", file=sys.stderr)
        return 2

    workspace = Path(args.workspace).resolve() if getattr(args, "workspace", None) else (base / "workspace")
    bridge = RuntimeBridgeClient.discover(workspace)

    # 1. Action: export
    if action == "export":
        doc_id = str(args.doc_id or "").strip()
        out_dir = str(args.out_dir or "").strip() or None
        force = bool(getattr(args, "force", False))
        try:
            res = None
            if bridge is not None:
                log.info("Runtime bridge detected at %s (pid %s); delegating export", bridge.base_url, bridge.pid)
                try:
                    res = BridgeWorkbench(bridge).export_agent_task_bundle(doc_id, out_dir=out_dir, force=force)
                except Exception as b_exc:
                    log.warning("Bridge delegation failed (%s), falling back to direct service execution", b_exc)
                    res = None
            if res is None:
                services = _init_services(base, config)
                try:
                    res = services["workbench"].export_agent_task_bundle(doc_id, out_dir=out_dir, force=force)
                finally:
                    _close_services(services)
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0 if res.get("ok") else 1
        except Exception as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
            return 1

    # 2. Action: submit
    elif action == "submit":
        doc_id = str(args.doc_id or "").strip()
        result_file = getattr(args, "result_file", None)
        result_json = getattr(args, "result_json", None)
        agent_name = str(getattr(args, "agent_name", "agent") or "agent")
        agent_model = str(getattr(args, "agent_model", "") or "")

        payload_source: str | Path | dict[str, Any]
        if result_json:
            payload_source = result_json
        elif result_file:
            target_path = Path(result_file)
            if not target_path.is_file():
                print(
                    json.dumps({"ok": False, "error": f"Result file not found: {result_file}"}, ensure_ascii=False),
                    file=sys.stderr,
                )
                return 1
            payload_source = target_path
        else:
            default_path = workspace / "agent_tasks" / doc_id / "result.json"
            if default_path.is_file():
                payload_source = default_path
            else:
                print(
                    json.dumps(
                        {"ok": False, "error": f"No result file provided and default {default_path} not found"},
                        ensure_ascii=False,
                    ),
                    file=sys.stderr,
                )
                return 1

        try:
            res = None
            if bridge is not None:
                log.info("Runtime bridge detected at %s (pid %s); delegating submit", bridge.base_url, bridge.pid)
                if isinstance(payload_source, Path):
                    payload_source = payload_source.read_text(encoding="utf-8")
                try:
                    res = BridgeWorkbench(bridge).submit_agent_task_result(
                        doc_id,
                        payload_source,
                        agent_name=agent_name,
                        agent_model=agent_model,
                    )
                except Exception as b_exc:
                    log.warning("Bridge delegation failed (%s), falling back to direct service execution", b_exc)
                    res = None
            if res is None:
                services = _init_services(base, config)
                try:
                    res = services["workbench"].submit_agent_task_result(
                        doc_id,
                        payload_source,
                        agent_name=agent_name,
                        agent_model=agent_model,
                    )
                finally:
                    _close_services(services)
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0 if res.get("ok") else 1
        except Exception as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
            return 1

    # 3. Action: status
    elif action == "status":
        doc_id = str(args.doc_id or "").strip()
        try:
            res = None
            if bridge is not None:
                log.info("Runtime bridge detected at %s (pid %s); delegating status", bridge.base_url, bridge.pid)
                try:
                    res = BridgeWorkbench(bridge).get_agent_task_status(doc_id)
                except Exception as b_exc:
                    log.warning("Bridge delegation failed (%s), falling back to direct service execution", b_exc)
                    res = None
            if res is None:
                services = _init_services(base, config)
                try:
                    res = services["workbench"].get_agent_task_status(doc_id)
                finally:
                    _close_services(services)
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0 if res.get("ok") else 1
        except Exception as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
            return 1

    # 4. Action: verify (Strict integrity & schema validation without writing to DB)
    elif action == "verify":
        doc_id = str(args.doc_id or "").strip()
        result_file = args.result_file
        try:
            services = _init_services(base, config)
            try:
                res = services["workbench"].verify_agent_task_result(doc_id, result_file)
            finally:
                _close_services(services)

            print(json.dumps(res, ensure_ascii=False, indent=2))
            if res.get("passed"):
                return 0
            return 1
        except Exception as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
            return 1

    # 5. Action: execute (with auto-pause and rate-limit polling resilience)
    elif action == "execute":
        doc_id = str(args.doc_id or "").strip()
        poll_interval = int(getattr(args, "poll_interval", 300) or 300)
        agent_name = str(getattr(args, "agent_name", "agent") or "agent")
        agent_model = str(getattr(args, "agent_model", "") or "")
        force = bool(getattr(args, "force", False))
        auto_submit = not bool(getattr(args, "no_submit", False))

        try:
            res = None
            if bridge is not None:
                log.info("Runtime bridge detected at %s (pid %s); delegating execute", bridge.base_url, bridge.pid)
                try:
                    res = BridgeWorkbench(bridge).execute_agent_task(
                        doc_id,
                        poll_interval_sec=poll_interval,
                        agent_name=agent_name,
                        agent_model=agent_model,
                        force=force,
                        auto_submit=auto_submit,
                    )
                except Exception as b_exc:
                    log.warning("Bridge delegation failed (%s), falling back to direct service execution", b_exc)
                    res = None
            if res is None:
                services = _init_services(base, config)
                try:
                    res = services["workbench"].execute_agent_task(
                        doc_id,
                        poll_interval_sec=poll_interval,
                        agent_name=agent_name,
                        agent_model=agent_model,
                        force=force,
                        auto_submit=auto_submit,
                    )
                finally:
                    _close_services(services)
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0 if res.get("ok") else 1
        except Exception as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
            return 1

    else:
        print(f"Error: Unknown agent action: {action}", file=sys.stderr)
        return 2


def _run_mcp(base: Path, config: AppConfig, args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).resolve() if getattr(args, "workspace", None) else (base / "workspace")
    bridge = RuntimeBridgeClient.discover(workspace)
    if bridge is not None:
        # A running GUI owns Qdrant; proxy every tool call to its live retriever
        # instead of opening the local vector DB and hitting the lock.
        log.info(
            "GUI runtime bridge detected at %s (pid %s); MCP uses full vector retrieval via proxy",
            bridge.base_url,
            bridge.pid or "?",
        )
        mcp_server = KnowledgeMCPServer(
            BridgeDB(bridge),
            BridgeRetriever(bridge),
            workbench=BridgeWorkbench(bridge),
        )
        if args.mcp_sse:
            host = str(args.host or config.get("mcp.host", "127.0.0.1"))
            port = int(args.port or config.get("mcp.port", 8765))
            mcp_server.run("sse", host=host, port=port)
        else:
            mcp_server.run("stdio")
        return 0

    services = _init_services(base, config)
    mcp_server: KnowledgeMCPServer = services["mcp_server"]
    try:
        if args.mcp_sse:
            host = str(args.host or config.get("mcp.host", "127.0.0.1"))
            port = int(args.port or config.get("mcp.port", 8765))
            log.info("Starting MCP SSE server on %s:%s", host, port)
            mcp_server.run("sse", host=host, port=port)
        else:
            log.info("Starting MCP stdio server")
            mcp_server.run("stdio")
    finally:
        try:
            services["qdrant"].close()
        except Exception:
            pass
        try:
            services["db"].close()
        except Exception:
            pass
    return 0


def start_gui_app(base_dir: str) -> int:
    base = Path(base_dir)
    setup_logging(base)
    log.info(
        "Starting AtomicCalculationDB V2.3.10 from %s (runtime home: %s)",
        base,
        getattr(sys, "_MEIPASS", str(base / "app_core")),
    )
    args = _parse_cli(sys.argv)
    if getattr(args, "base", None):
        base = Path(args.base).resolve()
    if getattr(args, "workspace", None):
        workspace = Path(args.workspace).resolve()
    else:
        workspace = base / "workspace"
    config = _load_config(workspace)

    if args.mcp or args.mcp_sse:
        return _run_mcp(base, config, args)

    if getattr(args, "subcommand", None) == "agent":
        return _run_agent_cli(base, config, args)

    services = _init_services(base, config)
    db = services["db"]
    qdrant = services["qdrant"]
    gateway = services["gateway"]
    worker = services["worker"]
    watcher = services["watcher"]
    stop_event = threading.Event()
    watcher_thread = watcher.start_polling(interval=30.0, stop_event=stop_event)

    version = str(read_version(base / "app_core").get("version", "2.6.0"))
    gui_workbench = AgentWorkbenchService(
        db,
        workspace,
        pipeline=services["pipeline"],
        config=config,
        reload_config_from_disk=False,
    )
    bridge: RuntimeBridgeServer | None = None
    if bool(config.get("runtime_bridge.enabled", True)):
        bridge = RuntimeBridgeServer(
            services["retriever"],
            db,
            workspace,
            host=str(config.get("runtime_bridge.host", "127.0.0.1") or "127.0.0.1"),
            port=int(config.get("runtime_bridge.port", 8765) or 8765),
            version=version,
            workbench=gui_workbench,
        )
        if not bridge.start():
            bridge = None

    _prepare_app_id()
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("AtomicCalculationDB")
    app.setOrganizationName("AtomicCalculationDB")
    app.setApplicationVersion(version)
    app.setWindowIcon(app_icon())
    app.setQuitOnLastWindowClosed(True)
    apply_theme(app)

    window = MainWindow(
        base,
        config,
        db,
        gateway=gateway,
        retriever=services["retriever"],
        worker=worker,
        fetcher=services["fetcher"],
        packager=services["packager"],
        sync_manager=services["sync_manager"],
        mcp_server=services["mcp_server"],
        pipeline=services["pipeline"],
        watcher=watcher,
        qdrant=qdrant,
        runtime_bridge=bridge,
    )
    gui_workbench.on_data_changed = window.external_data_changed.emit
    window.show()

    if bool(config.get("night_shift.enabled", True)):
        worker.start(force=False)

    auto_patch = os.environ.get("ACDB_AUTO_HOT_RESTART", "").strip()
    if auto_patch:
        def _auto_hot_restart() -> None:
            try:
                log.info("Smoke hook: staging %s and restarting", auto_patch)
                window.patch.stage(auto_patch)
                window.perform_hot_restart()
            except Exception:
                log.exception("Smoke hot-restart hook failed")

        QTimer.singleShot(1500, _auto_hot_restart)

    smoke_exit = os.environ.get("ACDB_SMOKE_EXIT_MS", "").strip()
    if smoke_exit:
        try:
            QTimer.singleShot(int(smoke_exit), app.quit)
            log.info("Smoke-exit timer armed for %s ms", smoke_exit)
        except ValueError:
            log.warning("Invalid ACDB_SMOKE_EXIT_MS=%r", smoke_exit)

    cleaned = {"done": False}

    def cleanup() -> None:
        if cleaned["done"]:
            return
        cleaned["done"] = True
        log.info("Shutting down")
        stop_event.set()
        try:
            worker.stop(graceful=False)
        except Exception:
            pass
        try:
            window.sync_view.shutdown()
        except Exception:
            pass
        try:
            watcher_thread.join(timeout=2)
        except Exception:
            pass
        if bridge is not None:
            bridge.stop()
        if qdrant is not None:
            qdrant.close()
        try:
            db.close()
        except Exception:
            log.exception("Database close failed")

    app.aboutToQuit.connect(cleanup)

    def excepthook(exc_type, exc, tb) -> None:
        detail = "".join(traceback.format_exception(exc_type, exc, tb))
        log.critical("Unhandled exception:\n%s", detail)
        QMessageBox.critical(
            window,
            "发生未处理的错误",
            "程序遇到错误，详情已写入 workspace/logs/app.log。\n\n" + str(exc)[:800],
        )

    sys.excepthook = excepthook
    try:
        return int(app.exec())
    finally:
        cleanup()


def main(argv: list[str] | None = None) -> int:
    release_root = os.environ.get("ACDB_RELEASE_ROOT", "").strip()
    if release_root:
        base = Path(release_root).resolve()
    else:
        base = Path(__file__).resolve().parent.parent
    return start_gui_app(str(base))

