"""Shared workbench & audit ticket service for MCP agents and runtime bridge.

Both standalone ``--mcp`` servers and the in-GUI ``RuntimeBridgeServer`` call
this service so Antigravity / Codex can discover pending documents, pull
token-budgeted extraction batches or v2.4.0 audit packets, submit validated
replies, and (when explicitly enabled in settings) write results directly to
the production knowledge base without fighting over Qdrant or SQLite locks.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from audit.importer import (
    AuditImportError,
    _extract_json as extract_audit_json,
    apply_audit_payload,
    preview_audit_payload,
)
from audit.report import export_audit_bundle
from config import AppConfig
from models import semi_auto

log = logging.getLogger(__name__)


class WorkbenchPermissionError(PermissionError):
    pass


class WorkbenchInputError(ValueError):
    pass


def _safe_slug(value: str, default: str = "agent") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip())
    return cleaned[:48] or default


class AgentWorkbenchService:
    """Thread-safe coordinator for MCP task/audit tickets."""

    def __init__(
        self,
        db: Any,
        workspace: str | Path,
        *,
        pipeline: Any = None,
        config: Any = None,
        on_data_changed: Callable[[], None] | None = None,
        reload_config_from_disk: bool = False,
    ):
        self.db = db
        self.workspace = Path(workspace)
        self.pipeline = pipeline
        self.config = config
        self.on_data_changed = on_data_changed
        self.reload_config_from_disk = reload_config_from_disk

    # ------------------------------------------------------------- permissions
    def _live_config(self) -> Any:
        if self.reload_config_from_disk:
            path = getattr(self.config, "path", None) or (self.workspace / "config.yaml")
            if Path(path).is_file():
                try:
                    return AppConfig.load(path, create=False)
                except Exception:
                    pass
        return self.config

    def permissions(self) -> dict[str, bool]:
        cfg = self._live_config()
        if cfg is None:
            return {"allow_task_write": True, "allow_direct_db_write": False}
        getter = getattr(cfg, "get", None)
        if callable(getter):
            return {
                "allow_task_write": bool(getter("mcp.allow_task_write", True)),
                "allow_direct_db_write": bool(getter("mcp.allow_direct_db_write", False)),
            }
        mcp = cfg.get("mcp", {}) if isinstance(cfg, Mapping) else {}
        return {
            "allow_task_write": bool(mcp.get("allow_task_write", True)),
            "allow_direct_db_write": bool(mcp.get("allow_direct_db_write", False)),
        }

    def _require_task_write(self) -> dict[str, bool]:
        perms = self.permissions()
        if not perms["allow_task_write"]:
            raise WorkbenchPermissionError(
                "MCP 任务工单接口已关闭（mcp.allow_task_write=false）。"
                "请在 GUI「同步与 MCP / 设置」开启后再调用。"
            )
        return perms

    def _require_direct_db_write(self) -> dict[str, bool]:
        perms = self._require_task_write()
        if not perms["allow_direct_db_write"]:
            raise WorkbenchPermissionError(
                "当前未开启「允许 Agent 通过 MCP 直接写入正式知识库」（mcp.allow_direct_db_write=false）。"
                "结果已安全暂存；请用户在 GUI 确认写入，或在设置中开启直接写库开关。"
            )
        return perms

    def _notify_changed(self) -> None:
        if self.on_data_changed is not None:
            try:
                self.on_data_changed()
            except Exception:
                log.debug("on_data_changed callback failed", exc_info=True)

    # --------------------------------------------------------------- discovery
    def list_workbench_tasks(
        self, *, status: str = "", limit: int = 20
    ) -> dict[str, Any]:
        capped = max(1, min(int(limit or 20), 100))
        wanted = str(status or "").strip().upper()
        all_docs = self.db.list_documents(limit=500)
        docs_out: list[dict[str, Any]] = []
        for doc in all_docs:
            parse_status = str(doc.get("parse_status") or "").upper()
            error_code = str(doc.get("parse_error_code") or "").upper()
            if wanted == "NEEDS_OCR":
                if error_code != "NEEDS_OCR":
                    continue
            elif wanted and wanted != "ALL":
                if parse_status != wanted:
                    continue
            elif not wanted:
                # Default view focuses on actionable documents.
                if parse_status == "LLM_EXTRACTED":
                    continue
            cards = self.db.list_knowledge_cards(
                filters={"doc_id": doc["doc_id"]}, limit=500
            )
            docs_out.append(
                {
                    "doc_id": doc["doc_id"],
                    "title": doc.get("title") or "",
                    "doc_type": doc.get("doc_type") or "PAP",
                    "pub_year": doc.get("pub_year"),
                    "download_status": doc.get("download_status") or "",
                    "parse_status": parse_status,
                    "parse_error_code": error_code,
                    "has_pdf": bool(doc.get("local_pdf_path")),
                    "has_markdown": bool(doc.get("local_md_path")),
                    "card_count": len(cards),
                }
            )
            if len(docs_out) >= capped:
                break

        runs = self.list_semi_auto_runs(include_finalized=False)
        exports = []
        if hasattr(self.db, "list_audit_exports"):
            for item in self.db.list_audit_exports(limit=10):
                exports.append(
                    {
                        "report_id": item.get("report_id"),
                        "doc_id": item.get("doc_id"),
                        "card_count": item.get("card_count"),
                        "part_count": item.get("part_count"),
                        "exported_at": item.get("exported_at"),
                        "export_dir": item.get("export_dir"),
                    }
                )
        return {
            "permissions": self.permissions(),
            "documents": docs_out,
            "unfinished_semi_auto_runs": runs,
            "recent_audit_exports": exports,
        }

    # --------------------------------------------------------- parse & batches
    def parse_document_for_agent(self, doc_id: str) -> dict[str, Any]:
        self._require_task_write()
        if self.pipeline is None:
            raise RuntimeError("Pipeline 未初始化，无法执行解析")
        doc_key = str(doc_id or "").strip().upper()
        if not doc_key:
            raise WorkbenchInputError("doc_id 不能为空")
        doc = self.db.get_document(doc_key)
        if not doc:
            raise WorkbenchInputError(f"找不到文献：{doc_key}")
        parsed = self.pipeline.parse_document(doc_key)
        refreshed = self.db.get_document(doc_key) or doc
        self._notify_changed()
        if parsed is None:
            return {
                "ok": False,
                "doc_id": doc_key,
                "parse_status": refreshed.get("parse_status"),
                "parse_error_code": refreshed.get("parse_error_code") or "",
                "error_log": refreshed.get("error_log") or "解析失败",
                "bundles_count": 0,
            }
        _parsed_doc, bundles, bundles_path = parsed
        return {
            "ok": True,
            "doc_id": doc_key,
            "parse_status": refreshed.get("parse_status"),
            "parse_error_code": refreshed.get("parse_error_code") or "",
            "bundles_count": len(bundles),
            "bundles_path": str(bundles_path),
            "markdown_backend": (refreshed.get("metadata") or {}).get(
                "markdown_backend", "pymupdf"
            ),
        }

    @staticmethod
    def _summarize_run(manifest: Mapping[str, Any]) -> dict[str, Any]:
        batches = []
        for batch in manifest.get("batches") or []:
            batches.append(
                {
                    "batch_id": batch.get("batch_id"),
                    "slot": batch.get("slot"),
                    "status": batch.get("status"),
                    "estimated_tokens": batch.get("estimated_tokens"),
                    "item_count": len(batch.get("item_ids") or []),
                    "doc_ids": batch.get("doc_ids") or [],
                    "imported_by": batch.get("imported_by") or "",
                }
            )
        return {
            "run_id": manifest.get("run_id"),
            "status": manifest.get("status"),
            "mode": manifest.get("mode"),
            "max_batch_tokens": manifest.get("max_batch_tokens"),
            "created_at": manifest.get("created_at"),
            "documents": manifest.get("documents") or [],
            "batches": batches,
            "ready_to_finalize": bool(batches)
            and all(
                str(b.get("status") or "").upper() == "IMPORTED" for b in batches
            ),
        }

    def list_semi_auto_runs(
        self, *, include_finalized: bool = False
    ) -> list[dict[str, Any]]:
        runs = []
        for manifest in semi_auto.list_runs(self.workspace):
            if (
                not include_finalized
                and str(manifest.get("status") or "").upper() == "FINALIZED"
            ):
                continue
            runs.append(self._summarize_run(manifest))
        return runs

    def create_semi_auto_batch_run(
        self,
        doc_ids: Iterable[str],
        *,
        mode: str = "incremental",
        max_batch_tokens: int = 25_000,
    ) -> dict[str, Any]:
        self._require_task_write()
        cleaned = [str(item).strip().upper() for item in doc_ids if str(item).strip()]
        if not cleaned:
            raise WorkbenchInputError("请至少提供一个 doc_id")
        docs = []
        missing = []
        for doc_id in cleaned:
            doc = self.db.get_document(doc_id)
            if not doc:
                missing.append(doc_id)
            else:
                docs.append(doc)
        if missing:
            raise WorkbenchInputError(f"找不到文献：{', '.join(missing)}")
        try:
            manifest = semi_auto.create_run(
                self.db,
                self.workspace,
                docs,
                self.pipeline,
                mode="force" if str(mode).lower() == "force" else "incremental",
                max_batch_tokens=max(2000, int(max_batch_tokens or 25_000)),
            )
        except semi_auto.SemiAutoError as exc:
            raise WorkbenchInputError(str(exc)) from exc
        return self._summarize_run(manifest)

    def _find_run_and_batch(
        self, run_id: str, batch_id: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        manifest = semi_auto.load_run(self.workspace, str(run_id or "").strip())
        if not manifest:
            raise WorkbenchInputError(f"找不到任务运行：{run_id}")
        target = next(
            (
                item
                for item in manifest.get("batches") or []
                if str(item.get("batch_id") or "") == str(batch_id or "").strip()
            ),
            None,
        )
        if target is None:
            raise WorkbenchInputError(f"在 {run_id} 中找不到批次：{batch_id}")
        return manifest, target

    def get_semi_auto_batch_prompt(
        self, run_id: str, batch_id: str
    ) -> dict[str, Any]:
        self._require_task_write()
        manifest, batch = self._find_run_and_batch(run_id, batch_id)
        try:
            prompt = semi_auto.read_prompt(self.workspace, manifest, batch)
            items = semi_auto.read_items(self.workspace, manifest, batch)
        except semi_auto.SemiAutoError as exc:
            raise WorkbenchInputError(str(exc)) from exc
        return {
            "run_id": manifest["run_id"],
            "batch_id": batch["batch_id"],
            "status": batch.get("status"),
            "estimated_tokens": batch.get("estimated_tokens"),
            "doc_ids": batch.get("doc_ids") or [],
            "item_ids": [str(item.get("item_id") or "") for item in items],
            "prompt": prompt,
        }

    def submit_semi_auto_batch_reply(
        self,
        run_id: str,
        batch_id: str,
        reply_json: str,
        *,
        agent_name: str = "antigravity",
        agent_model: str = "",
    ) -> dict[str, Any]:
        perms = self._require_task_write()
        manifest, batch = self._find_run_and_batch(run_id, batch_id)
        if str(manifest.get("status") or "").upper() == "FINALIZED":
            raise WorkbenchInputError(f"任务 {run_id} 已写入知识库，不能覆盖回复")
        try:
            outcome = semi_auto.import_reply(
                self.workspace,
                manifest,
                batch,
                str(reply_json or ""),
                source_label=str(agent_name or "antigravity"),
                agent_model=str(agent_model or ""),
            )
        except semi_auto.SemiAutoError as exc:
            raise WorkbenchInputError(str(exc)) from exc
        ready = str(manifest.get("status") or "").upper() == "READY"
        return {
            **outcome,
            "run_id": manifest["run_id"],
            "run_status": manifest.get("status"),
            "ready_to_finalize": ready,
            "allow_direct_db_write": perms["allow_direct_db_write"],
        }

    def finalize_semi_auto_run(
        self,
        run_id: str,
        *,
        agent_name: str = "antigravity",
        agent_model: str = "",
    ) -> dict[str, Any]:
        self._require_direct_db_write()
        if self.pipeline is None:
            raise RuntimeError("Pipeline 未初始化，无法写入知识库")
        manifest = semi_auto.load_run(self.workspace, str(run_id or "").strip())
        if not manifest:
            raise WorkbenchInputError(f"找不到任务运行：{run_id}")
        try:
            result = semi_auto.finalize_run(
                self.pipeline,
                self.db,
                self.workspace,
                manifest,
                agent_name=str(agent_name or "antigravity"),
                agent_model=str(agent_model or ""),
            )
        except semi_auto.SemiAutoError as exc:
            raise WorkbenchInputError(str(exc)) from exc
        self._notify_changed()
        return {
            "run_id": manifest["run_id"],
            **result,
        }

    # ----------------------------------------------------------- audit tickets
    def export_document_audit_ticket(
        self,
        doc_id: str,
        *,
        include_source: bool = True,
        include_evidence: bool = True,
        coverage_audit: bool = False,
    ) -> dict[str, Any]:
        self._require_task_write()
        doc_key = str(doc_id or "").strip().upper()
        if not doc_key or self.db.get_document(doc_key) is None:
            raise WorkbenchInputError(f"找不到文献：{doc_key or '—'}")
        exported = export_audit_bundle(
            self.db,
            self.workspace,
            doc_key,
            include_source=bool(include_source),
            include_evidence=bool(include_evidence),
            coverage_audit=bool(coverage_audit),
        )
        return {
            "report_id": exported.report_id,
            "report_hash": exported.report_hash,
            "doc_id": doc_key,
            "card_count": exported.card_count,
            "part_count": exported.part_count,
            "export_dir": str(exported.export_dir),
            "source_included": exported.source_included,
            "files": [path.name for path in exported.files],
        }

    def _resolve_audit_export(self, report_id: str) -> tuple[dict[str, Any], Path]:
        key = str(report_id or "").strip()
        if not key:
            raise WorkbenchInputError("report_id 不能为空")
        record = self.db.audit_export_for_report(key)
        if record is None:
            raise WorkbenchInputError(f"找不到审计导出记录：{key}")
        export_dir = Path(str(record.get("export_dir") or ""))
        if not export_dir.is_dir():
            raise WorkbenchInputError(f"审计导出目录不存在：{export_dir}")
        return record, export_dir

    def get_audit_ticket_content(
        self,
        report_id: str,
        *,
        part: int = 1,
        include_prompt: bool = True,
    ) -> dict[str, Any]:
        self._require_task_write()
        record, export_dir = self._resolve_audit_export(report_id)
        manifest = dict(record.get("manifest") or {})
        parts = list(manifest.get("parts") or [])
        part_num = max(1, int(part or 1))
        part_meta = next(
            (item for item in parts if int(item.get("part") or 0) == part_num),
            None,
        )
        if part_meta is None:
            raise WorkbenchInputError(
                f"报告 {report_id} 没有第 {part_num} 片（共 {len(parts)} 片）"
            )
        report_file = export_dir / str(part_meta.get("file") or "report.md")
        prompt_file = export_dir / str(part_meta.get("prompt") or "audit_prompt.md")
        report_md = (
            report_file.read_text(encoding="utf-8")
            if report_file.is_file()
            else ""
        )
        prompt_md = (
            prompt_file.read_text(encoding="utf-8")
            if include_prompt and prompt_file.is_file()
            else ""
        )
        source_meta = manifest.get("source") or {}
        source_path = export_dir / str(source_meta.get("file") or "source.md")
        return {
            "report_id": record.get("report_id"),
            "report_hash": record.get("report_hash"),
            "doc_id": record.get("doc_id"),
            "part": part_num,
            "part_total": len(parts),
            "batch_id": part_meta.get("batch_id"),
            "card_ids": part_meta.get("card_ids") or [],
            "export_dir": str(export_dir),
            "source_file": str(source_path) if source_path.is_file() else "",
            "prompt": prompt_md,
            "report_markdown": report_md,
        }

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
        perms = self._require_task_write()
        record, export_dir = self._resolve_audit_export(report_id)
        raw_text = str(audit_json or "").strip()
        if not raw_text:
            raise WorkbenchInputError("audit_json 不能为空")
        try:
            payload = extract_audit_json(raw_text)
        except AuditImportError as exc:
            raise WorkbenchInputError(str(exc)) from exc
        if not isinstance(payload, dict):
            raise WorkbenchInputError("审计 JSON 顶层必须是对象")
        payload.setdefault("document_id", record.get("doc_id"))
        payload.setdefault("report_id", record.get("report_id"))
        payload.setdefault("report_hash", record.get("report_hash"))
        payload["_file_hash"] = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        staged_name = f"agent_audit_{_safe_slug(auditor_name)}_{stamp}.json"
        staged_path = export_dir / staged_name
        payload["_source_file"] = staged_name
        try:
            preview = preview_audit_payload(
                self.db,
                payload,
                document_id=str(record.get("doc_id") or ""),
                auditor_name=str(auditor_name or "antigravity"),
                auditor_model=str(auditor_model or ""),
                auditor_provider="mcp",
                auditor_node_id=f"mcp-{_safe_slug(auditor_name).lower()}",
                audit_source="mcp_agent",
            )
        except AuditImportError as exc:
            raise WorkbenchInputError(str(exc)) from exc

        staged_payload = {
            k: v for k, v in payload.items() if not str(k).startswith("_")
        }
        staged_path.write_text(
            json.dumps(staged_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        if not apply_to_db:
            return {
                "report_id": preview.report_id,
                "doc_id": preview.document_id,
                "staged_file": str(staged_path),
                "applied_to_db": False,
                "allow_direct_db_write": perms["allow_direct_db_write"],
                "stats": preview.stats,
                "warnings": preview.warnings,
            }

        if not perms["allow_direct_db_write"]:
            return {
                "report_id": preview.report_id,
                "doc_id": preview.document_id,
                "staged_file": str(staged_path),
                "applied_to_db": False,
                "allow_direct_db_write": False,
                "requires_setting": "mcp.allow_direct_db_write",
                "stats": preview.stats,
                "warnings": preview.warnings,
                "message": (
                    "审计 JSON 已校验并暂存到导出目录；由于未开启「允许 Agent 直接写入正式库」，"
                    "尚未改动数据库。可在 GUI 审计导入中直接载入该文件，或在设置中开启直接写库开关。"
                ),
            }

        applied = apply_audit_payload(
            self.db,
            preview,
            apply_verified=bool(apply_verified),
        )
        # Keep vector DB in sync when pipeline is available; otherwise the
        # VECTORIZE queue entries created by apply_audit_preview will retry.
        if self.pipeline is not None and (applied.modified or applied.added):
            for item in preview.reviews:
                if (item.get("payload") or {}).get("suggested_action") == "modify":
                    try:
                        self.pipeline.reindex_card(str(item.get("knw_id") or ""))
                    except Exception:
                        pass
        self._notify_changed()
        return {
            "report_id": preview.report_id,
            "doc_id": preview.document_id,
            "staged_file": str(staged_path),
            "applied_to_db": True,
            "stats": preview.stats,
            "apply_result": {
                "applied": applied.applied,
                "modified": applied.modified,
                "deleted": applied.deleted,
                "added": applied.added,
                "kept": applied.kept,
                "needs_review": applied.needs_review,
                "rejected": applied.rejected,
                "duplicates": applied.duplicates,
            },
            "warnings": applied.warnings,
        }

    # ------------------------------------------------------------------ step 5 deep synthesis
    def export_deep_synthesis_task(self, doc_id: str) -> dict[str, Any]:
        """Export Step 5 deep synthesis prompt & L2 cards for a document."""
        self._require_task_write()
        from models.deep_synthesis import export_master_synthesis_prompt
        try:
            return export_master_synthesis_prompt(self.db, doc_id)
        except ValueError as exc:
            raise WorkbenchInputError(str(exc)) from exc

    def submit_deep_synthesis_result(
        self,
        doc_id: str,
        master_json: str | dict[str, Any],
        *,
        agent_name: str = "antigravity",
        agent_model: str = "",
        apply_to_db: bool = True,
    ) -> dict[str, Any]:
        """Submit Step 5 L1 Master Card synthesis result. Controlled by mcp.allow_direct_db_write."""
        perms = self._require_task_write()
        from models.deep_synthesis import apply_master_synthesis_payload
        from models.atomic_prompts import parse_master_json, MasterCardDraft

        if isinstance(master_json, str):
            try:
                draft = parse_master_json(master_json)
            except Exception as exc:
                raise WorkbenchInputError(f"Master 卡片解析失败: {exc}") from exc
        elif isinstance(master_json, dict):
            try:
                draft = MasterCardDraft.model_validate(master_json)
            except Exception as exc:
                raise WorkbenchInputError(f"Master 卡片校验失败: {exc}") from exc
        else:
            raise WorkbenchInputError("master_json 必须为 JSON 字符串或 dict")

        if not apply_to_db:
            return {
                "doc_id": doc_id,
                "applied_to_db": False,
                "allow_direct_db_write": perms["allow_direct_db_write"],
                "draft": draft.model_dump(),
                "message": "Master 卡片校验成功，未写入数据库 (apply_to_db=False)。",
            }

        if not perms["allow_direct_db_write"]:
            return {
                "doc_id": doc_id,
                "applied_to_db": False,
                "allow_direct_db_write": False,
                "requires_setting": "mcp.allow_direct_db_write",
                "draft": draft.model_dump(),
                "message": (
                    "Master 卡片校验成功；由于未开启「允许 Agent 直接写入正式库」，"
                    "尚未写入数据库。可在设置中开启直接写库开关后重试。"
                ),
            }

        qdrant = getattr(self.pipeline, "qdrant", None)
        provenance = {
            "agent_name": str(agent_name or "antigravity"),
            "agent_model": str(agent_model or ""),
            "agent_provider": "mcp",
            "source_kind": "MCP_AGENT",
        }
        res = apply_master_synthesis_payload(
            self.db,
            qdrant,
            doc_id,
            draft.model_dump(),
            source_kind="MCP_AGENT",
            provenance=provenance,
        )
        self._notify_changed()
        return {
            "doc_id": doc_id,
            "applied_to_db": True,
            "knw_id": res["knw_id"],
            "title_zh": res["title_zh"],
            "glow": "GOLD",
            "message": "L1 Master 卡片已成功写入数据库并同步向量库，文献进度条已点亮金光！",
        }

    # -------------------------------------------------- agent task bundle (v3.1.0)
    def export_agent_task_bundle(
        self,
        doc_id: str,
        *,
        out_dir: str | Path | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Export a self-explaining, decoupled task bundle folder for Agent processing."""
        self._require_task_write()
        from sync_mcp.agent_task_packager import AgentTaskPackager

        try:
            packager = AgentTaskPackager(self.db, self.workspace, pipeline=self.pipeline)
            return packager.export_task_bundle(doc_id, out_dir=out_dir, force=force)
        except ValueError as exc:
            raise WorkbenchInputError(str(exc)) from exc
        except Exception as exc:
            log.exception("export_agent_task_bundle failed for %s", doc_id)
            raise

    def submit_agent_task_result(
        self,
        doc_id: str,
        result_path_or_json: str | dict[str, Any],
        *,
        agent_name: str = "antigravity",
        agent_model: str = "",
    ) -> dict[str, Any]:
        """Submit Agent task extraction results (validates schema, creates L2 cards, updates status)."""
        self._require_task_write()
        from sync_mcp.agent_task_packager import AgentTaskPackager

        try:
            packager = AgentTaskPackager(self.db, self.workspace, pipeline=self.pipeline)
            res = packager.submit_task_result(
                doc_id,
                result_path_or_json,
                agent_name=agent_name,
                agent_model=agent_model,
            )
            self._notify_changed()
            return res
        except ValueError as exc:
            raise WorkbenchInputError(str(exc)) from exc
        except Exception as exc:
            log.exception("submit_agent_task_result failed for %s", doc_id)
            raise

    def get_agent_task_status(self, doc_id: str) -> dict[str, Any]:
        """Check status of an agent task folder and doc extraction progress."""
        from sync_mcp.agent_task_packager import AgentTaskPackager

        try:
            packager = AgentTaskPackager(self.db, self.workspace, pipeline=self.pipeline)
            return packager.get_task_status(doc_id)
        except ValueError as exc:
            raise WorkbenchInputError(str(exc)) from exc
        except Exception as exc:
            log.exception("get_agent_task_status failed for %s", doc_id)
            raise



__all__ = [
    "AgentWorkbenchService",
    "WorkbenchInputError",
    "WorkbenchPermissionError",
]
