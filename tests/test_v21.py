import argparse
import hashlib
import json
import zipfile
from datetime import datetime
from pathlib import Path

import pytest

from builtin_models.model_registry import ModelRegistry
from crawler.mirror_probe import normalize_mirror, probe_mirrors
from db.hybrid_retriever import HybridRetriever
from db.sqlite_manager import SQLiteManager
from runtime_bridge import (
    BridgeDB,
    BridgeRetriever,
    RuntimeBridgeClient,
    RuntimeBridgeServer,
)
from scheduler.night_worker import NightWorker
from sync_mcp.acdb_packager import ACDBPackager
from sync_mcp.mcp_server import KnowledgeMCPServer
from ui.formula_renderer import (
    FormulaWidget,
    expand_physics_macros,
    render_formula_pixmap,
    split_aligned,
)


def _db(tmp_path: Path, signature: str = "test::dim=4") -> SQLiteManager:
    return SQLiteManager(tmp_path / "workspace", embedding_signature=signature)


# --------------------------------------------------------------- workbench
def test_workbench_same_day_cross_day_and_all_day(tmp_path: Path):
    db = _db(tmp_path)
    same = NightWorker(
        db,
        workspace_dir=str(tmp_path),
        config={"night_shift": {"enabled": True, "start": "13:00", "end": "17:00"}},
    )
    assert same.is_in_window(datetime(2026, 10, 31, 13, 0))
    assert same.is_in_window(datetime(2026, 10, 31, 16, 59))
    assert not same.is_in_window(datetime(2026, 10, 31, 17, 0))
    assert not same.is_in_window(datetime(2026, 10, 31, 12, 59))

    overnight = NightWorker(
        db,
        workspace_dir=str(tmp_path),
        config={"night_shift": {"enabled": True, "start": "18:00", "end": "08:00"}},
    )
    assert overnight.is_in_window(datetime(2026, 10, 31, 18, 0))
    assert overnight.is_in_window(datetime(2026, 11, 1, 0, 30))
    assert overnight.is_in_window(datetime(2026, 11, 1, 7, 59))
    assert not overnight.is_in_window(datetime(2026, 11, 1, 8, 0))
    assert not overnight.is_in_window(datetime(2026, 11, 1, 12, 0))

    all_day = NightWorker(
        db,
        workspace_dir=str(tmp_path),
        config={"night_shift": {"enabled": True, "start": "00:00", "end": "00:00"}},
    )
    assert all_day.always_on
    assert all_day.is_in_window(datetime(2026, 10, 31, 3, 0))
    assert all_day.is_in_window(datetime(2026, 10, 31, 23, 59))

    disabled = NightWorker(
        db,
        workspace_dir=str(tmp_path),
        config={"night_shift": {"enabled": False, "start": "00:00", "end": "00:00"}},
    )
    assert not disabled.is_in_window(datetime(2026, 10, 31, 12, 0))
    db.close()


def test_graceful_stop_generates_workbench_report(tmp_path: Path):
    db = _db(tmp_path)
    worker = NightWorker(
        db,
        workspace_dir=str(tmp_path),
        config={"night_shift": {"enabled": True, "start": "00:00", "end": "00:00"}},
        poll_seconds=0.05,
    )
    assert worker.start(force=True)
    worker.stop(graceful=True, generate_report=True, timeout=3)
    reports = list((tmp_path / "morning_reports").glob("Report_*.md"))
    assert reports
    assert "工作台运行简报" in reports[0].read_text(encoding="utf-8")
    db.close()


# ----------------------------------------------------------------- formula
def test_formula_macros_and_high_dpi_render(qapp):
    expanded = expand_physics_macros(
        r"\bra{\psi}\hat{H}\ket{\phi} = E \braket{\psi}{\phi}"
    )
    assert r"\langle \psi" in expanded
    assert r"\rangle" in expanded
    assert r"\bra" not in expanded

    aligned = (
        r"\begin{aligned} H &= H_D + B_{ij} "
        + chr(92) * 2
        + r" E &= mc^2 \end{aligned}"
    )
    rows = split_aligned(aligned)
    assert len(rows) == 2
    pixmap = render_formula_pixmap(r"\hat{H}\ket{\psi} = E\psi", max_width=500)
    assert pixmap is not None and pixmap.width() > 10
    widget = FormulaWidget(aligned, max_width=480)
    from PySide6.QtWidgets import QPushButton

    assert widget.latex
    assert any(
        button.text() == "复制 LaTeX" for button in widget.findChildren(QPushButton)
    )


# ------------------------------------------------------------- ONNX model
def test_model_urls_import_and_download_all(tmp_path: Path, monkeypatch):
    registry = ModelRegistry(tmp_path / "workspace")
    spec = registry.spec("mini_cluster_embed")
    url = registry._resolve_url("https://hf-mirror.com", spec.repo_id, spec.remote_filename)
    assert url.startswith("https://hf-mirror.com/Xenova/all-MiniLM-L6-v2/resolve/main/")
    assert url.endswith("model_quantized.onnx")

    folder = tmp_path / "models-in"
    folder.mkdir()
    for name in ("mini_cluster_embed", "conflict_nli", "local_reranker"):
        item = registry.spec(name)
        (folder / item.filename).write_bytes(b"onnx-dummy")
        (folder / item.tokenizer_filename).write_text(
            json.dumps({"model": {"type": "WordPiece"}}), encoding="utf-8"
        )
    result = registry.import_from_folder(folder)
    assert set(result["imported"]) == {
        "mini_cluster_embed",
        "conflict_nli",
        "local_reranker",
    }
    assert registry.is_present("conflict_nli")

    def fake_download(spec, *, progress, mirror_bases):
        registry.path_for(spec.name).write_bytes(b"onnx")
        registry.tokenizer_path(spec.name).write_text(
            json.dumps({"model": {"type": "WordPiece"}}), encoding="utf-8"
        )
        if progress:
            progress({"event": "done", "label": spec.name})

    monkeypatch.setattr(registry, "_download_from_hf", fake_download)
    download = registry.download_all(force=True)
    assert download["ok"] is True
    assert all(download["models"].values())


def test_download_file_hash_and_progress(tmp_path: Path, monkeypatch):
    registry = ModelRegistry(tmp_path / "workspace")
    payload = b"onnx payload" * 100
    digest = hashlib.sha256(payload).hexdigest()

    class Response:
        status_code = 200
        headers = {
            "content-length": str(len(payload)),
            "etag": f'"{digest}"',
        }

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def raise_for_status(self):
            return None

        def iter_bytes(self, size=256):
            for index in range(0, len(payload), size):
                yield payload[index : index + size]

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def head(self, url):
            return Response()

        def stream(self, method, url):
            return Response()

    import httpx

    monkeypatch.setattr(httpx, "Client", Client)
    events = []
    target = tmp_path / "workspace" / "models" / "x.onnx"
    registry._download_file(
        "https://example.invalid/model.onnx",
        target,
        progress=events.append,
        label="x",
        min_bytes=10,
    )
    assert target.read_bytes() == payload
    assert target.with_suffix(".onnx.sha256").read_text().strip() == digest
    assert any(event.get("event") == "progress" for event in events)
    assert any(event.get("event") == "done" for event in events)


# -------------------------------------------------------- cloud snapshot
def test_cloud_snapshot_incremental_hot_merge(tmp_path: Path):
    source_dir = tmp_path / "source"
    source = SQLiteManager(source_dir, embedding_signature="test::dim=4")
    doc_id = source.upsert_document(
        {
            "doc_id": "DOC-PAP-2024-ABCDEF",
            "title": "Cloud merged paper",
            "pub_year": 2024,
            "download_status": "DOWNLOADED",
        }
    )
    card_id = source.upsert_knowledge_card(
        {
            "knw_id": "KNW-STRAT-2024-ABCDEF",
            "card_type": "STRAT",
            "title_zh": "云端策略卡",
            "summary_zh": "云端活性空间策略",
            "supporting_doc_ids": [doc_id],
            "consensus_count": 7,
        }
    )
    source.add_evidence(card_id, doc_id, "⟪FOCUS⟫ merged evidence", page_num=2)
    kw_id = source.upsert_keyword("active space", term_zh="活性空间")
    source.link_card_keywords(card_id, [kw_id])
    snapshot = tmp_path / "remote.sqlite"
    source.backup_to(snapshot)
    source.close()

    target_dir = tmp_path / "target"
    target = SQLiteManager(target_dir, embedding_signature="test::dim=4")
    target.upsert_document({"title": "Local only paper"})
    target.upsert_knowledge_card(
        {
            "knw_id": card_id,
            "card_type": "STRAT",
            "title_zh": "旧版本地卡",
            "summary_zh": "旧结论",
            "supporting_doc_ids": [doc_id],
            "consensus_count": 1,
        }
    )
    packager = ACDBPackager(target, target_dir)
    counts = packager.merge_snapshot(snapshot, vectorizer=lambda card: [1.0, 0.0, 0.0, 0.0])
    assert counts["documents_added"] == 1
    assert counts["cards_updated"] == 1
    assert counts["evidence_added"] == 1
    merged = target.get_knowledge_card(card_id)
    assert merged["consensus_count"] == 7
    assert target.list_evidence(card_id)
    assert target.keywords_for_card(card_id)
    assert target.count_documents() == 2
    target.close()


# --------------------------------------------------------------- bridge
def test_runtime_bridge_roundtrip_and_mcp_proxy(tmp_path: Path, monkeypatch):
    db = _db(tmp_path)
    doc_id = db.upsert_document({"title": "Bridge paper", "pub_year": 2024})
    card_id = db.upsert_knowledge_card(
        {
            "card_type": "STRAT",
            "title_zh": "Bridge MCDHF strategy",
            "summary_zh": "MCDHF active space strategy for Fe XVII",
            "supporting_doc_ids": [doc_id],
            "search_payload": "MCDHF active space Fe XVII strategy",
        }
    )
    db.add_evidence(card_id, doc_id, "⟪FOCUS⟫ bridge evidence", page_num=1)
    retriever = HybridRetriever(db)
    server = RuntimeBridgeServer(
        retriever, db, tmp_path / "workspace", port=0, version="2.1.0"
    )
    assert server.start()
    client = RuntimeBridgeClient.discover(tmp_path / "workspace")
    assert client is not None and client.alive()
    hits = BridgeRetriever(client).search("active space", limit=5)
    assert any(hit.id == card_id for hit in hits)
    assert BridgeDB(client).get_knowledge_card(card_id)["title_zh"] == "Bridge MCDHF strategy"
    assert BridgeDB(client).list_evidence(card_id)

    # A --mcp process must take the proxy path without opening SQLite/Qdrant.
    import main_entry

    calls = []

    def forbidden_services(*args, **kwargs):
        raise AssertionError("services must not be opened when the GUI bridge is alive")

    monkeypatch.setattr(main_entry, "_init_services", forbidden_services)
    monkeypatch.setattr(
        main_entry.KnowledgeMCPServer,
        "run",
        lambda self, transport="stdio", host="127.0.0.1", port=8765: calls.append(
            (transport, type(self.db).__name__, type(self.retriever).__name__)
        ),
    )
    config = main_entry.AppConfig({})
    args = argparse.Namespace(mcp=True, mcp_sse=False, host=None, port=None)
    base = tmp_path
    assert main_entry._run_mcp(base, config, args) == 0
    assert calls == [("stdio", "BridgeDB", "BridgeRetriever")]

    server.stop()
    assert not (tmp_path / "workspace" / "runtime_bridge.json").exists()
    db.close()


# ---------------------------------------------------------- mirror probe
def test_mirror_normalization_and_probe():
    assert normalize_mirror("mirror.example/path") == "https://mirror.example"
    assert normalize_mirror("ftp://bad.example") == ""

    class Response:
        status_code = 200

    class Client:
        def get(self, url, headers=None):
            if "down" in url:
                raise RuntimeError("offline")
            return Response()

    results = probe_mirrors(
        ["https://up.example", "https://down.example"], client=Client()
    )
    assert results[0]["ok"] is True
    assert results[1]["ok"] is False


# ------------------------------------------------------------- updater
def test_updater_accepts_one_layer_app_core_zip(tmp_path: Path):
    from updater.updater import apply_pending_update, inspect_package, pending_path, stage_package

    base = tmp_path / "release"
    core = base / "app_core"
    core.mkdir(parents=True)
    (core / "version.json").write_text(json.dumps({"version": "2.0.0"}), encoding="utf-8")
    (core / "main_entry.py").write_text("OLD = True\n", encoding="utf-8")
    (base / "workspace").mkdir()
    package = tmp_path / "patch.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("app_core/version.json", json.dumps({"version": "2.1.1"}))
        archive.writestr("app_core/main_entry.py", "NEW = True\n")
        archive.writestr("app_core/db/__init__.py", "")
    info = inspect_package(package)
    assert info.prefix == "app_core/"
    stage_package(base, package)
    applied = apply_pending_update(base)
    assert applied is not None and applied.version == "2.1.1"
    assert (core / "main_entry.py").read_text(encoding="utf-8") == "NEW = True\n"
    assert (core / "db" / "__init__.py").exists()
    assert not pending_path(base).exists()


def test_gui_hot_restart_releases_resources(qapp, tmp_path: Path, monkeypatch):
    from config import AppConfig
    from main_entry import _init_services
    from ui.main_window import MainWindow, apply_theme

    base = tmp_path / "release"
    (base / "app_core").mkdir(parents=True)
    (base / "app_core" / "version.json").write_text(
        json.dumps({"version": "2.1.0", "build": "test"}), encoding="utf-8"
    )
    (base / "workspace").mkdir()
    config = AppConfig.load(base / "workspace" / "config.yaml")
    services = _init_services(base, config)
    apply_theme(qapp)
    window = MainWindow(
        base,
        config,
        services["db"],
        gateway=services["gateway"],
        retriever=services["retriever"],
        worker=services["worker"],
        fetcher=services["fetcher"],
        packager=services["packager"],
        sync_manager=services["sync_manager"],
        mcp_server=services["mcp_server"],
        pipeline=services["pipeline"],
        watcher=services["watcher"],
        qdrant=services["qdrant"],
    )
    closed = {"qdrant": False, "db": False, "relaunch": False}
    original_qdrant_close = services["qdrant"].close
    original_db_close = services["db"].close
    monkeypatch.setattr(window.qdrant, "close", lambda: closed.__setitem__("qdrant", True))
    monkeypatch.setattr(window.db, "close", lambda: closed.__setitem__("db", True))
    monkeypatch.setattr(window.patch, "relaunch", lambda: closed.__setitem__("relaunch", True))
    window.perform_hot_restart()
    assert closed == {"qdrant": True, "db": True, "relaunch": True}
    window.close()
    original_qdrant_close()
    original_db_close()
