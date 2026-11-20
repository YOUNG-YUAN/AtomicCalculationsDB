"""FastMCP / MCPServer integration.

Exposes the knowledge base to Claude Desktop, Cursor, Cherry Studio, etc.
Works with both the v1 ``FastMCP`` import and the v2 ``MCPServer`` import.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from db.hybrid_retriever import HybridRetriever

log = logging.getLogger(__name__)

try:  # MCP v1
    from mcp.server.fastmcp import FastMCP as _MCPImpl  # type: ignore
except Exception:  # MCP v2
    try:
        from mcp.server.mcpserver import MCPServer as _MCPImpl  # type: ignore
    except Exception:  # pragma: no cover
        _MCPImpl = None


class MCPUnavailable(RuntimeError):
    pass


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, indent=2)


class KnowledgeMCPServer:
    def __init__(
        self,
        db: Any,
        retriever: HybridRetriever,
        *,
        workbench: Any = None,
    ):
        self.db = db
        self.retriever = retriever
        self.workbench = workbench
        self._server: Any = None

    @property
    def available(self) -> bool:
        return _MCPImpl is not None

    def status(self) -> dict[str, Any]:
        tools = [
            "query_calculation_strategy",
            "explain_atomic_model",
            "lookup_by_id",
            "get_code_template",
            "search_knowledge",
            "get_raw_evidence",
        ]
        if self.workbench is not None:
            tools.extend(
                [
                    "list_workbench_tasks",
                    "parse_document_for_agent",
                    "create_semi_auto_batch_run",
                    "list_semi_auto_runs",
                    "get_semi_auto_batch_prompt",
                    "submit_semi_auto_batch_reply",
                    "finalize_semi_auto_run",
                    "export_document_audit_ticket",
                    "get_audit_ticket_content",
                    "submit_audit_ticket_result",
                    "export_deep_synthesis_task",
                    "submit_deep_synthesis_result",
                    "export_agent_task_bundle",
                    "submit_agent_task_result",
                    "get_agent_task_status",
                    "execute_agent_task",
                ]
            )
        return {
            "available": self.available,
            "impl": getattr(_MCPImpl, "__name__", "missing"),
            "tools": tools,
        }

    def build_server(self):
        if _MCPImpl is None:
            raise MCPUnavailable("mcp 包未安装")
        self._server = _MCPImpl("AtomicCalculationsDB")
        server = self._server

        @server.tool()
        def query_calculation_strategy(
            element_or_ion: str, process: str = "", method: str = ""
        ) -> str:
            """Query extraction/calculation strategies for an ion, process and method."""
            query = " ".join(part for part in (element_or_ion, process, method) if part)
            hits = self.retriever.search(query, limit=10, include_evidence=False)
            return _json([hit.as_dict() for hit in hits])

        @server.tool()
        def explain_atomic_model(model_name: str, aspect: str = "") -> str:
            """Explain a physical model (Hamiltonian, approximations, applicability)."""
            query = f"{model_name} {aspect} 模型".strip()
            hits = self.retriever.search(query, limit=8, include_evidence=False)
            return _json(
                [
                    {
                        **hit.as_dict(),
                        "formulas": (hit.card or {}).get("formulas_latex", ""),
                        "premises": (hit.card or {}).get(
                            "premises_and_conditions", ""
                        ),
                    }
                    for hit in hits
                ]
            )

        @server.tool()
        def lookup_by_id(entity_id: str) -> str:
            """Look up a DOC-/KNW-/MASTER-/KW-/EV- ID exactly."""
            hit = self.retriever.lookup(entity_id)
            return _json(hit.as_dict() if hit else None)

        @server.tool()
        def get_code_template(program_name: str, task_type: str = "") -> str:
            """Retrieve program workflow/input templates (GRASP, FAC, AMBiT...)."""
            query = f"{program_name} {task_type} 代码 模板 workflow".strip()
            hits = self.retriever.search(query, limit=8)
            return _json(
                [
                    {
                        "knw_id": hit.id,
                        "title": hit.title,
                        "code_snippet": (hit.card or {}).get("code_snippet", ""),
                        "summary": hit.summary,
                    }
                    for hit in hits
                    if (hit.card or {}).get("card_type") == "CODE"
                    or (hit.card or {}).get("code_snippet")
                ]
            )

        @server.tool()
        def search_knowledge(query: str, limit: int = 10) -> str:
            """Hybrid (FTS + vector + rerank) search over L1/L2 cards."""
            hits = self.retriever.search(query, limit=max(1, min(int(limit), 30)))
            return _json([hit.as_dict() for hit in hits])

        @server.tool()
        def get_raw_evidence(knw_id: str) -> str:
            """Return L3 context bundles / page references for a knowledge card."""
            card = self.db.get_knowledge_card(knw_id)
            if not card:
                return _json(None)
            return _json(
                {
                    "knw_id": card["knw_id"],
                    "title": card.get("title_zh"),
                    "evidence": self.db.list_evidence(knw_id),
                }
            )

        def _wb_call(fn_name: str, **kwargs: Any) -> str:
            if self.workbench is None:
                return _json({"ok": False, "error": "工作台工单服务未初始化"})
            try:
                result = getattr(self.workbench, fn_name)(**kwargs)
                return _json({"ok": True, "data": result})
            except Exception as exc:
                return _json({"ok": False, "error": str(exc)})

        @server.tool()
        def list_workbench_tasks(status: str = "", limit: int = 20) -> str:
            """List documents needing parse/extraction, unfinished semi-auto runs, and recent audit exports."""
            return _wb_call("list_workbench_tasks", status=status, limit=limit)

        @server.tool()
        def parse_document_for_agent(doc_id: str) -> str:
            """Parse/OCR a document and generate bundles.json without calling LLM extraction."""
            return _wb_call("parse_document_for_agent", doc_id=doc_id)

        @server.tool()
        def create_semi_auto_batch_run(
            doc_ids: list[str],
            mode: str = "incremental",
            max_batch_tokens: int = 25000,
        ) -> str:
            """Create a persistent token-budgeted semi-auto extraction run for one or more DOC-IDs."""
            return _wb_call(
                "create_semi_auto_batch_run",
                doc_ids=doc_ids,
                mode=mode,
                max_batch_tokens=max_batch_tokens,
            )

        @server.tool()
        def list_semi_auto_runs(include_finalized: bool = False) -> str:
            """List semi-auto batch runs and per-batch statuses."""
            return _wb_call("list_semi_auto_runs", include_finalized=include_finalized)

        @server.tool()
        def get_semi_auto_batch_prompt(run_id: str, batch_id: str) -> str:
            """Fetch the prompt text, item IDs, and DOC-IDs for one semi-auto batch."""
            return _wb_call("get_semi_auto_batch_prompt", run_id=run_id, batch_id=batch_id)

        @server.tool()
        def submit_semi_auto_batch_reply(
            run_id: str,
            batch_id: str,
            reply_json: str,
            agent_name: str = "antigravity",
            agent_model: str = "",
        ) -> str:
            """Validate and stage an extraction reply JSON for a semi-auto batch (does not modify formal DB)."""
            return _wb_call(
                "submit_semi_auto_batch_reply",
                run_id=run_id,
                batch_id=batch_id,
                reply_json=reply_json,
                agent_name=agent_name,
                agent_model=agent_model,
            )

        @server.tool()
        def finalize_semi_auto_run(
            run_id: str,
            agent_name: str = "antigravity",
            agent_model: str = "",
        ) -> str:
            """Commit all imported batches of a semi-auto run into the knowledge base (requires mcp.allow_direct_db_write)."""
            return _wb_call(
                "finalize_semi_auto_run",
                run_id=run_id,
                agent_name=agent_name,
                agent_model=agent_model,
            )

        @server.tool()
        def export_document_audit_ticket(
            doc_id: str,
            include_source: bool = True,
            include_evidence: bool = True,
            coverage_audit: bool = False,
        ) -> str:
            """Export a v2.4.0 cross-model audit bundle for a document and return its report_id and metadata."""
            return _wb_call(
                "export_document_audit_ticket",
                doc_id=doc_id,
                include_source=include_source,
                include_evidence=include_evidence,
                coverage_audit=coverage_audit,
            )

        @server.tool()
        def get_audit_ticket_content(
            report_id: str,
            part: int = 1,
            include_prompt: bool = True,
        ) -> str:
            """Read the audit prompt and report markdown for a specific report_id and part."""
            return _wb_call(
                "get_audit_ticket_content",
                report_id=report_id,
                part=part,
                include_prompt=include_prompt,
            )

        @server.tool()
        def submit_audit_ticket_result(
            report_id: str,
            audit_json: str,
            auditor_name: str = "antigravity",
            auditor_model: str = "",
            apply_to_db: bool = False,
            apply_verified: bool = False,
        ) -> str:
            """Validate and stage a v2.4.0 audit JSON; optionally apply to DB when mcp.allow_direct_db_write is enabled."""
            return _wb_call(
                "submit_audit_ticket_result",
                report_id=report_id,
                audit_json=audit_json,
                auditor_name=auditor_name,
                auditor_model=auditor_model,
                apply_to_db=apply_to_db,
                apply_verified=apply_verified,
            )

        @server.tool()
        def export_deep_synthesis_task(doc_id: str) -> str:
            """Export Step 5 deep synthesis prompt & L2 detail cards for a document."""
            return _wb_call("export_deep_synthesis_task", doc_id=doc_id)

        @server.tool()
        def submit_deep_synthesis_result(
            doc_id: str,
            master_json: str,
            agent_name: str = "antigravity",
            agent_model: str = "",
            apply_to_db: bool = True,
        ) -> str:
            """Submit Step 5 L1 Master Card synthesis result and trigger GOLD glow when written to DB."""
            return _wb_call(
                "submit_deep_synthesis_result",
                doc_id=doc_id,
                master_json=master_json,
                agent_name=agent_name,
                agent_model=agent_model,
                apply_to_db=apply_to_db,
            )

        @server.tool()
        def export_agent_task_bundle(
            doc_id: str,
            out_dir: str = "",
            force: bool = False,
        ) -> str:
            """Export a self-explaining, decoupled task bundle folder for Agent processing (items.json, tables, schema)."""
            kwargs: dict[str, Any] = {"doc_id": doc_id, "force": force}
            if out_dir:
                kwargs["out_dir"] = out_dir
            return _wb_call("export_agent_task_bundle", **kwargs)

        @server.tool()
        def submit_agent_task_result(
            doc_id: str,
            result_json: str,
            agent_name: str = "antigravity",
            agent_model: str = "",
        ) -> str:
            """Submit Agent task extraction results (validates schema, creates L2 cards, updates status)."""
            return _wb_call(
                "submit_agent_task_result",
                doc_id=doc_id,
                result_path_or_json=result_json,
                agent_name=agent_name,
                agent_model=agent_model,
            )

        @server.tool()
        def get_agent_task_status(doc_id: str) -> str:
            """Check status of an agent task folder and doc extraction progress."""
            return _wb_call("get_agent_task_status", doc_id=doc_id)

        @server.tool()
        def execute_agent_task(
            doc_id: str,
            poll_interval_sec: int = 300,
            agent_name: str = "antigravity",
            agent_model: str = "",
            force: bool = False,
            auto_submit: bool = True,
        ) -> str:
            """Execute agent task with auto-pause & rate-limit polling resilience (every 5-10m on 429/timeout)."""
            return _wb_call(
                "execute_agent_task",
                doc_id=doc_id,
                poll_interval_sec=poll_interval_sec,
                agent_name=agent_name,
                agent_model=agent_model,
                force=force,
                auto_submit=auto_submit,
            )


        return server

    @property
    def server(self):
        return self._server or self.build_server()

    def run(self, transport: str = "stdio", host: str = "127.0.0.1", port: int = 8765) -> None:
        server = self.server
        try:
            server.run(transport=transport, host=host, port=port)
        except TypeError:
            # Some MCP versions read host/port from constructor settings.
            server.run(transport=transport)


def build_mcp_config(exe_path: str | Path) -> dict[str, Any]:
    return {
        "mcpServers": {
            "atomic-calculation-db": {
                "command": str(exe_path),
                "args": ["--mcp"],
            }
        }
    }
