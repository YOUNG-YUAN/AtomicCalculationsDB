"""Loopback retrieval bridge shared by the GUI and external MCP processes.

Qdrant local mode takes an exclusive directory lock.  When the GUI is running
it already owns the only ``HybridRetriever`` (SQLite + Qdrant + reranker), so a
``--mcp`` process talks to that live instance over a token-protected loopback
HTTP endpoint instead of opening the vector database a second time.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping

log = logging.getLogger(__name__)

BRIDGE_FILENAME = "runtime_bridge.json"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_BODY_BYTES = 16 * 1024 * 1024


class BridgeUnavailable(RuntimeError):
    pass


def bridge_file(workspace: str | Path) -> Path:
    return Path(workspace) / BRIDGE_FILENAME


@dataclass
class BridgeHit:
    kind: str
    id: str
    title: str
    summary: str = ""
    score: float = 0.0
    card: dict[str, Any] | None = None
    document: dict[str, Any] | None = None
    keyword: dict[str, Any] | None = None
    snippet: str = ""
    evidence: list[dict[str, Any]] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "BridgeHit":
        return cls(
            kind=str(data.get("kind") or "card"),
            id=str(data.get("id") or ""),
            title=str(data.get("title") or ""),
            summary=str(data.get("summary") or ""),
            score=float(data.get("score") or 0.0),
            card=data.get("card"),
            document=data.get("document"),
            keyword=data.get("keyword"),
            snippet=str(data.get("snippet") or ""),
            evidence=list(data.get("evidence") or []),
            scores=dict(data.get("scores") or {}),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "id": self.id,
            "title": self.title,
            "summary": self.summary,
            "score": self.score,
            "card": self.card,
            "document": self.document,
            "keyword": self.keyword,
            "snippet": self.snippet,
            "evidence": self.evidence,
            "scores": self.scores,
        }


class RuntimeBridgeServer:
    """Threaded HTTP server that exposes the GUI's live retriever."""

    def __init__(
        self,
        retriever: Any,
        db: Any,
        workspace: str | Path,
        *,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        version: str = "",
        workbench: Any = None,
    ):
        self.retriever = retriever
        self.db = db
        self.workspace = Path(workspace)
        self.host = host
        self.requested_port = int(port)
        self.version = version
        self.workbench = workbench
        self.token = secrets.token_urlsafe(32)
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port = 0

    # ---------------------------------------------------------------- lifecycle
    def start(self, *, max_port_tries: int = 100) -> bool:
        if self._httpd is not None:
            return True
        handler = self._make_handler()
        last_error: Exception | None = None
        for offset in range(max_port_tries):
            port = self.requested_port + offset
            if port > 65535:
                break
            try:
                httpd = ThreadingHTTPServer((self.host, port), handler)
            except OSError as exc:
                last_error = exc
                continue
            httpd.daemon_threads = True
            self._httpd = httpd
            self.port = int(httpd.server_address[1])
            self._thread = threading.Thread(
                target=httpd.serve_forever,
                name="acdb-runtime-bridge",
                daemon=True,
            )
            self._thread.start()
            self._write_state()
            log.info("Runtime bridge listening on %s:%s", self.host, self.port)
            return True
        log.warning("Runtime bridge could not bind: %s", last_error)
        return False

    def stop(self) -> None:
        httpd = self._httpd
        self._httpd = None
        if httpd is not None:
            try:
                httpd.shutdown()
            except Exception:
                pass
            try:
                httpd.server_close()
            except Exception:
                pass
        if self._thread is not None:
            try:
                self._thread.join(timeout=3)
            except Exception:
                pass
            self._thread = None
        try:
            state = self._read_state()
            if state.get("token") == self.token:
                bridge_file(self.workspace).unlink(missing_ok=True)
        except Exception:
            pass

    def _write_state(self) -> None:
        data = {
            "format": "acdb-runtime-bridge/1",
            "host": self.host,
            "port": self.port,
            "token": self.token,
            "pid": os.getpid(),
            "version": self.version,
            "started_at": _now(),
        }
        path = bridge_file(self.workspace)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def _read_state(self) -> dict[str, Any]:
        try:
            return json.loads(bridge_file(self.workspace).read_text(encoding="utf-8"))
        except Exception:
            return {}

    # ------------------------------------------------------------------ RPC
    def _make_handler(self):
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "ACDBBridge/1.0"

            def log_message(self, *args) -> None:  # silence request spam
                return

            def _json(self, status: int, payload: Any) -> None:
                data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except BrokenPipeError:
                    pass

            def do_GET(self) -> None:  # noqa: N802
                if self.path.rstrip("/") in ("/health", "/ping"):
                    self._json(
                        200,
                        {
                            "ok": True,
                            "service": "acdb-runtime-bridge",
                            "pid": os.getpid(),
                            "version": bridge.version,
                            "collection": getattr(
                                getattr(bridge.retriever, "qdrant", None),
                                "collection",
                                "",
                            ),
                            "vector_available": bool(
                                getattr(
                                    getattr(bridge.retriever, "qdrant", None),
                                    "available",
                                    False,
                                )
                            ),
                        },
                    )
                else:
                    self._json(404, {"ok": False, "error": "not found"})

            def do_POST(self) -> None:  # noqa: N802
                if self.path.rstrip("/") != "/rpc":
                    self._json(404, {"ok": False, "error": "not found"})
                    return
                if self.headers.get("X-ACDB-Token") != bridge.token:
                    self._json(403, {"ok": False, "error": "forbidden"})
                    return
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = 0
                if length <= 0 or length > MAX_BODY_BYTES:
                    self._json(400, {"ok": False, "error": "invalid body size"})
                    return
                try:
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                    result = bridge.dispatch(
                        str(body.get("method") or ""),
                        list(body.get("args") or []),
                        dict(body.get("kwargs") or {}),
                    )
                except BridgeUnavailable as exc:
                    self._json(400, {"ok": False, "error": str(exc)})
                    return
                except Exception as exc:
                    log.exception("Runtime bridge RPC failed")
                    self._json(500, {"ok": False, "error": str(exc)})
                    return
                self._json(200, {"ok": True, "result": result})

        return Handler

    def dispatch(self, method: str, args: list[Any], kwargs: dict[str, Any]) -> Any:
        if method == "search":
            query = str(kwargs.get("query") or (args[0] if args else ""))
            hits = self.retriever.search(
                query,
                limit=max(1, min(int(kwargs.get("limit") or 10), 50)),
                include_evidence=bool(kwargs.get("include_evidence", False)),
                extra_filters=kwargs.get("extra_filters") or None,
            )
            return [hit.as_dict() for hit in hits]
        if method == "lookup":
            entity_id = str(kwargs.get("entity_id") or (args[0] if args else ""))
            hit = self.retriever.lookup(entity_id)
            return hit.as_dict() if hit else None
        if method == "get_knowledge_card":
            return self.db.get_knowledge_card(
                str(kwargs.get("knw_id") or (args[0] if args else ""))
            )
        if method == "list_evidence":
            return self.db.list_evidence(
                str(kwargs.get("knw_id") or (args[0] if args else ""))
            )
        if method == "stats":
            return self.db.stats()
        if method == "workbench":
            if self.workbench is None:
                raise BridgeUnavailable("GUI 工作台桥接未初始化")
            action = str(kwargs.get("action") or (args[0] if args else ""))
            params = dict(kwargs.get("params") or {})
            allowed = {
                "permissions",
                "list_workbench_tasks",
                "parse_document_for_agent",
                "list_semi_auto_runs",
                "create_semi_auto_batch_run",
                "get_semi_auto_batch_prompt",
                "submit_semi_auto_batch_reply",
                "finalize_semi_auto_run",
                "export_document_audit_ticket",
                "get_audit_ticket_content",
                "submit_audit_ticket_result",
                "export_deep_synthesis_task",
                "submit_deep_synthesis_result",
            }
            if action not in allowed:
                raise BridgeUnavailable(f"unsupported workbench action: {action}")
            fn = getattr(self.workbench, action)
            return fn(**params)
        if method == "status":
            return {
                "pid": os.getpid(),
                "version": self.version,
                "vector_available": bool(
                    getattr(getattr(self.retriever, "qdrant", None), "available", False)
                ),
            }
        raise BridgeUnavailable(f"unsupported bridge method: {method}")


class RuntimeBridgeClient:
    def __init__(
        self,
        host: str,
        port: int,
        token: str,
        *,
        version: str = "",
        pid: int = 0,
    ):
        self.host = host
        self.port = int(port)
        self.token = token
        self.version = version
        self.pid = int(pid or 0)

    @classmethod
    def discover(cls, workspace: str | Path) -> "RuntimeBridgeClient | None":
        try:
            data = json.loads(bridge_file(workspace).read_text(encoding="utf-8"))
        except Exception:
            return None
        if data.get("format") != "acdb-runtime-bridge/1":
            return None
        client = cls(
            str(data.get("host") or DEFAULT_HOST),
            int(data.get("port") or 0),
            str(data.get("token") or ""),
            version=str(data.get("version") or ""),
            pid=int(data.get("pid") or 0),
        )
        return client if client.alive() else None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def alive(self) -> bool:
        if not self.token or not self.port:
            return False
        try:
            import httpx

            response = httpx.get(self.base_url + "/health", timeout=0.8)
            return response.status_code == 200 and bool(response.json().get("ok"))
        except Exception:
            return False

    def call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        try:
            import httpx

            response = httpx.post(
                self.base_url + "/rpc",
                headers={"X-ACDB-Token": self.token},
                json={"method": method, "args": list(args), "kwargs": kwargs},
                timeout=120.0,
            )
        except Exception as exc:
            raise BridgeUnavailable(f"无法连接 GUI 检索桥: {exc}") from exc
        if response.status_code != 200:
            raise BridgeUnavailable(
                f"GUI 检索桥返回 {response.status_code}: {response.text[:200]}"
            )
        payload = response.json()
        if not payload.get("ok"):
            raise BridgeUnavailable(str(payload.get("error") or "bridge error"))
        return payload.get("result")


class BridgeRetriever:
    def __init__(self, client: RuntimeBridgeClient):
        self.client = client
        self.qdrant = None

    def search(self, query: str, **kwargs: Any) -> list[BridgeHit]:
        output = self.client.call("search", query=query, **kwargs)
        return [BridgeHit.from_dict(item) for item in output or []]

    def lookup(self, entity_id: str) -> BridgeHit | None:
        output = self.client.call("lookup", entity_id=entity_id)
        return BridgeHit.from_dict(output) if output else None


class BridgeDB:
    def __init__(self, client: RuntimeBridgeClient):
        self.client = client

    def get_knowledge_card(self, knw_id: str) -> dict[str, Any] | None:
        return self.client.call("get_knowledge_card", knw_id=knw_id)

    def list_evidence(self, knw_id: str) -> list[dict[str, Any]]:
        return self.client.call("list_evidence", knw_id=knw_id) or []

    def stats(self) -> dict[str, Any]:
        return self.client.call("stats") or {}


class BridgeWorkbench:
    """Proxy AgentWorkbenchService calls to the live GUI process."""

    def __init__(self, client: RuntimeBridgeClient):
        self.client = client

    def _call(self, action: str, **params: Any) -> Any:
        return self.client.call("workbench", action=action, params=params)

    def permissions(self) -> dict[str, bool]:
        return self._call("permissions")

    def list_workbench_tasks(self, *, status: str = "", limit: int = 20) -> dict[str, Any]:
        return self._call("list_workbench_tasks", status=status, limit=limit)

    def parse_document_for_agent(self, doc_id: str) -> dict[str, Any]:
        return self._call("parse_document_for_agent", doc_id=doc_id)

    def list_semi_auto_runs(self, *, include_finalized: bool = False) -> list[dict[str, Any]]:
        return self._call("list_semi_auto_runs", include_finalized=include_finalized)

    def create_semi_auto_batch_run(
        self,
        doc_ids: list[str],
        *,
        mode: str = "incremental",
        max_batch_tokens: int = 25_000,
    ) -> dict[str, Any]:
        return self._call(
            "create_semi_auto_batch_run",
            doc_ids=list(doc_ids),
            mode=mode,
            max_batch_tokens=max_batch_tokens,
        )

    def get_semi_auto_batch_prompt(self, run_id: str, batch_id: str) -> dict[str, Any]:
        return self._call("get_semi_auto_batch_prompt", run_id=run_id, batch_id=batch_id)

    def submit_semi_auto_batch_reply(
        self,
        run_id: str,
        batch_id: str,
        reply_json: str,
        *,
        agent_name: str = "antigravity",
        agent_model: str = "",
    ) -> dict[str, Any]:
        return self._call(
            "submit_semi_auto_batch_reply",
            run_id=run_id,
            batch_id=batch_id,
            reply_json=reply_json,
            agent_name=agent_name,
            agent_model=agent_model,
        )

    def finalize_semi_auto_run(
        self,
        run_id: str,
        *,
        agent_name: str = "antigravity",
        agent_model: str = "",
    ) -> dict[str, Any]:
        return self._call(
            "finalize_semi_auto_run",
            run_id=run_id,
            agent_name=agent_name,
            agent_model=agent_model,
        )

    def export_document_audit_ticket(
        self,
        doc_id: str,
        *,
        include_source: bool = True,
        include_evidence: bool = True,
        coverage_audit: bool = False,
    ) -> dict[str, Any]:
        return self._call(
            "export_document_audit_ticket",
            doc_id=doc_id,
            include_source=include_source,
            include_evidence=include_evidence,
            coverage_audit=coverage_audit,
        )

    def get_audit_ticket_content(
        self,
        report_id: str,
        *,
        part: int = 1,
        include_prompt: bool = True,
    ) -> dict[str, Any]:
        return self._call(
            "get_audit_ticket_content",
            report_id=report_id,
            part=part,
            include_prompt=include_prompt,
        )

    def submit_audit_ticket_result(
        self,
        report_id: str,
        audit_json: str,
        *,
        auditor_name: str = "antigravity",
        auditor_model: str = "",
        apply_to_db: bool = False,
        apply_verified: bool = False,
    ) -> dict[str, Any]:
        return self._call(
            "submit_audit_ticket_result",
            report_id=report_id,
            audit_json=audit_json,
            auditor_name=auditor_name,
            auditor_model=auditor_model,
            apply_to_db=apply_to_db,
            apply_verified=apply_verified,
        )

    def export_deep_synthesis_task(self, doc_id: str) -> dict[str, Any]:
        return self._call("export_deep_synthesis_task", doc_id=doc_id)

    def submit_deep_synthesis_result(
        self,
        doc_id: str,
        master_json: str | dict[str, Any],
        *,
        agent_name: str = "antigravity",
        agent_model: str = "",
        apply_to_db: bool = True,
    ) -> dict[str, Any]:
        return self._call(
            "submit_deep_synthesis_result",
            doc_id=doc_id,
            master_json=master_json,
            agent_name=agent_name,
            agent_model=agent_model,
            apply_to_db=apply_to_db,
        )


def _now() -> str:
    from datetime import datetime

    return datetime.now().astimezone().isoformat(timespec="seconds")


__all__ = [
    "BRIDGE_FILENAME",
    "BridgeDB",
    "BridgeHit",
    "BridgeRetriever",
    "BridgeUnavailable",
    "BridgeWorkbench",
    "RuntimeBridgeClient",
    "RuntimeBridgeServer",
    "bridge_file",
]
