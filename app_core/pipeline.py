"""End-to-end task pipeline handlers for the night worker.

FETCH → PARSE → EXTRACT → CONSENSUS/VECTORIZE → REPORT

The pipeline is deliberately decoupled from Qt so it can be tested and used
from ``launcher.py --mcp`` or a headless worker.
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Mapping

from crawler.waterfall_fetcher import WaterfallFetcher
from db.hybrid_retriever import HybridRetriever
from db.qdrant_manager import QdrantManager
from db.sqlite_manager import SQLiteManager, build_search_payload
from models.atomic_prompts import KnowledgeCardDraft
from models.gateway import ModelGateway
from models.provenance import collect_pool_provenance
from parser.book_locator import BookLocator
from parser.consensus_conflict_engine import ConsensusConflictEngine
from parser.context_bundler import ContextBundler, ContextBundle, bundle_hash
from parser.card_quality import is_invalid_card, sanitize_card_fields
from parser.document_profile import (
    is_low_value_bundle,
    is_manual_document,
)
from parser.metadata_extractor import is_valid_title
from parser.four_channel_radar import FourChannelRadar
from parser.pdf_to_markdown import PDFParser, ParsedDocument, latex_source_to_markdown
from parser.mineru_backend import MinerUConfig, MinerUConverter

log = logging.getLogger(__name__)


def _cfg(config: Any, dotted: str, default: Any = None) -> Any:
    if config is None:
        return default
    if isinstance(config, Mapping):
        node: Any = config
        for part in dotted.split("."):
            if not isinstance(node, Mapping) or part not in node:
                return default
            node = node[part]
        return node
    getter = getattr(config, "get", None)
    return getter(dotted, default) if callable(getter) else default


def _compact_text_length(text: str) -> int:
    return len(re.sub(r"\s+", "", str(text or "")))


def _structured_with_conditions(draft: Any) -> dict[str, Any]:
    details = dict(getattr(draft, "structured_details", {}) or {})
    conditions = getattr(draft, "conditions", None)
    if conditions is not None:
        if hasattr(conditions, "model_dump"):
            details["conditions"] = conditions.model_dump()
        elif isinstance(conditions, Mapping):
            details["conditions"] = dict(conditions)
    return details


def _text_layer_too_sparse(parsed: ParsedDocument) -> bool:
    """Detect image-only/scanned PDFs before spending tokens on extraction."""
    metadata = getattr(parsed, "metadata", {}) or {}
    if str(metadata.get("markdown_backend") or "").lower() == "mineru":
        return False
    pages = int(getattr(parsed, "page_count", 0) or 0)
    if pages < 3:
        return False
    compact = _compact_text_length(getattr(parsed, "markdown", ""))
    return compact < max(200, 60 * pages)


class Pipeline:
    def __init__(
        self,
        db: SQLiteManager,
        workspace_dir: str | Path,
        *,
        config: Any = None,
        gateway: ModelGateway | None = None,
        qdrant: QdrantManager | None = None,
        retriever: HybridRetriever | None = None,
        fetcher: WaterfallFetcher | None = None,
        parser: PDFParser | None = None,
        radar: FourChannelRadar | None = None,
        bundler: ContextBundler | None = None,
        consensus: ConsensusConflictEngine | None = None,
    ):
        self.db = db
        self.workspace = Path(workspace_dir)
        self.config = config
        self.gateway = gateway
        self.qdrant = qdrant
        self.retriever = retriever
        self.fetcher = fetcher or WaterfallFetcher(
            db, self.workspace, config=config
        )
        mineru_config = MinerUConfig.from_config(config)
        self.mineru = MinerUConverter(self.workspace, mineru_config)
        self.parser = parser or PDFParser(
            mineru_backend=self.mineru,
            mineru_fallback=mineru_config.fallback_to_pymupdf,
        )
        if self.gateway is not None and getattr(self.gateway, "vlm_client", None) is not None:
            if getattr(self.gateway.vlm_client, "available", False):
                self.parser.ocr_callback = lambda image, prompt: self.gateway.vlm.describe(
                    image, prompt
                )
        self.radar = radar or FourChannelRadar()
        self.bundler = bundler or ContextBundler()
        self.consensus = consensus or ConsensusConflictEngine(
            registry=getattr(gateway, "registry", None)
        )

    # ------------------------------------------------------------- handlers
    def register_handlers(self, worker: Any) -> None:
        worker.register_handler("DISCOVER", self.handle_discover)
        worker.register_handler("FETCH", self.handle_fetch)
        worker.register_handler("PARSE", self.handle_parse)
        worker.register_handler("EXTRACT", self.handle_extract)
        worker.register_handler("VECTORIZE", self.handle_vectorize)
        worker.register_handler("CONSOLIDATE", self.handle_consolidate)
        worker.register_handler("REPORT", self.handle_report)

    def handle_discover(self, task: Mapping[str, Any]) -> bool:
        payload = _payload(task)
        query = payload.get("query") or "atomic structure MCDHF"
        self.fetcher.discover_and_queue(query)
        return True

    def handle_fetch(self, task: Mapping[str, Any]) -> bool:
        doc_id = task.get("doc_id") or _payload(task).get("doc_id")
        if not doc_id:
            return False
        result = self.fetcher.fetch(str(doc_id))
        if result.status == "DOWNLOADED":
            self.db.add_task("PARSE", doc_id=str(doc_id), priority=10)
            return True
        return result.status in ("ALREADY_PRESENT", "MANUAL_REQUIRED")

    def parse_document(
        self,
        doc_id: str,
        *,
        progress: Any = None,
        force_manual: bool | None = None,
    ) -> tuple[ParsedDocument, list[ContextBundle], Path] | None:
        """Parse a local PDF/LaTeX source, build bundles and return them.

        This is shared by the normal PARSE handler and the library's
        incremental/force reprocessing controls.
        """
        document = self.db.get_document(str(doc_id))
        if not document:
            return None
        pdf_path = self._local_path(document.get("local_pdf_path"))
        metadata = document.get("metadata") or {}
        source_archive = self._local_path(metadata.get("arxiv_source"))
        if progress:
            progress("读取本地文献", 0.08)
        if self.gateway is not None and hasattr(self.gateway, "set_active_document"):
            self.gateway.set_active_document(str(doc_id))
        try:
            if pdf_path is not None and pdf_path.exists():
                try:
                    parsed = self.parser.parse(pdf_path, doc_id=str(doc_id))
                except Exception as exc:
                    log.exception("PDF parse failed for %s", doc_id)
                    self.db.update_parse_status(
                        str(doc_id), "FAILED", str(exc), error_code="PARSE_ERROR"
                    )
                    return None
                directory = pdf_path.parent
            elif source_archive is not None and source_archive.exists():
                try:
                    markdown = latex_source_to_markdown(source_archive)
                    parsed = self.parser.parse_markdown(
                        markdown,
                        title=document.get("title", ""),
                        doc_id=str(doc_id),
                        source_path=str(source_archive),
                        metadata=metadata,
                    )
                except Exception as exc:
                    log.exception("LaTeX source parse failed for %s", doc_id)
                    self.db.update_parse_status(
                        str(doc_id), "FAILED", str(exc), error_code="PARSE_ERROR"
                    )
                    return None
                directory = source_archive.parent
            else:
                self.db.update_parse_status(
                    str(doc_id),
                    "FAILED",
                    "缺少本地 PDF 或 LaTeX 源",
                    error_code="MISSING_SOURCE",
                )
                return None
        finally:
            if self.gateway is not None and hasattr(self.gateway, "set_active_document"):
                self.gateway.set_active_document(None)

        if pdf_path is not None and _text_layer_too_sparse(parsed):
            compact = _compact_text_length(parsed.markdown)
            message = (
                f"PDF 文本层过少（{parsed.page_count} 页仅 {compact} 个有效字符，"
                "疑似扫描件）；正文未解析，也未调用大模型。"
                "请先用 OCR 工具为该 PDF 添加文本层，然后重新解析。"
            )
            self.db.update_parse_status(
                str(doc_id), "FAILED", message, error_code="NEEDS_OCR"
            )
            self.db.log_event(
                "ERROR", "parse", f"{doc_id} {message}", doc_id=str(doc_id)
            )
            return None

        manual = force_manual
        if manual is None:
            manual = is_manual_document(
                title=str(document.get("title") or parsed.title or ""),
                doc_type=str(document.get("doc_type") or ""),
                filename=pdf_path.name if pdf_path else "",
                section_titles=[section.title for section in parsed.sections],
            )
        if manual and str(document.get("doc_type") or "").upper() not in ("MAN", "BOK"):
            self.db.upsert_document({"doc_id": str(doc_id), "doc_type": "MAN"})
            document["doc_type"] = "MAN"
            self.db.log_event(
                "INFO", "parse", f"{doc_id} 识别为程序手册/实操指南", doc_id=str(doc_id)
            )

        # Long monographs are routed; manuals/guides are explicitly exempt so
        # their examples and workflow chapters are never cut off early.
        scan_document = parsed
        if not manual and (parsed.page_count >= 100 or len(parsed.sections) > 40):
            windows = BookLocator(self.db).locate(parsed, max_windows=15)
            if windows:
                allowed = {window.section_path for window in windows}
                scan_document = ParsedDocument(
                    title=parsed.title,
                    markdown=parsed.markdown,
                    sections=[
                        section
                        for section in parsed.sections
                        if section.path in allowed
                    ],
                    page_count=parsed.page_count,
                    metadata=parsed.metadata,
                    source_path=parsed.source_path,
                    doc_id=parsed.doc_id,
                )
                self.db.log_event(
                    "INFO",
                    "parse",
                    f"{doc_id} 长文献定位：{len(windows)} 个高密度小节 / 共 {len(parsed.sections)}",
                    doc_id=str(doc_id),
                )

        # Persist high-confidence title/journal/year/DOI from DOI lookup or
        # first-page layout analysis, without overwriting a curated title.
        meta = parsed.metadata or {}
        title_value = str(parsed.title or meta.get("title") or "").strip()
        updates: dict[str, Any] = {}
        current_title = str(document.get("title") or "")
        if title_value and is_valid_title(title_value) and (
            not is_valid_title(current_title)
            or str(meta.get("source") or "") in ("crossref", "openalex")
        ):
            updates["title"] = title_value
        if meta.get("journal") and not document.get("journal"):
            updates["journal"] = meta["journal"]
        if meta.get("year") and not document.get("pub_year"):
            updates["pub_year"] = meta["year"]
        if meta.get("doi") and not document.get("doi"):
            updates["doi"] = meta["doi"]
        if meta.get("authors") and not document.get("authors_list"):
            updates["authors"] = meta["authors"]
        backend_keys = (
            "markdown_backend",
            "mineru_version",
            "mineru_tier",
            "mineru_elapsed_sec",
            "mineru_output_dir",
            "mineru_fallback_error",
        )
        backend_meta = {key: meta[key] for key in backend_keys if key in meta}
        if backend_meta:
            merged_meta = dict(document.get("metadata") or {})
            merged_meta.update(backend_meta)
            updates["metadata_json"] = merged_meta
        if updates:
            updates["doc_id"] = str(doc_id)
            self.db.upsert_document(updates)
            document = self.db.get_document(str(doc_id)) or document
        backend = str(meta.get("markdown_backend") or "pymupdf")
        self.db.log_event(
            "INFO",
            "parse",
            f"{doc_id} Markdown 后端：{backend}"
            + (
                f" {meta.get('mineru_version')} / {meta.get('mineru_tier')}"
                if backend == "mineru"
                else ""
            ),
            doc_id=str(doc_id),
        )

        if progress:
            progress("构建四通道证据包", 0.45)
        md_path = directory / "parsed.md"
        md_path.write_text(parsed.markdown, encoding="utf-8")
        self.db.set_local_md_path(str(doc_id), md_path)
        radar = self.radar.scan(scan_document, manual=manual)
        max_bundles = 2000 if manual else 200
        bundles = self.bundler.build(
            scan_document, radar, doc_id=str(doc_id),
            max_bundles=max_bundles, manual=manual,
        )
        bundles_path = directory / "bundles.json"
        bundles_path.write_text(
            json.dumps([bundle.model_dump() for bundle in bundles], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        table_payload = []
        figure_payload = []
        for section in scan_document.sections:
            for table in section.tables:
                table_payload.append(
                    {
                        "section_path": section.path,
                        "block_id": table.block_id,
                        "caption": table.caption,
                        "page_num": table.page_num,
                        "bbox": list(table.bbox) if table.bbox else None,
                        "rows": table.rows,
                        "source_format": table.source_format,
                        "header_rows": table.header_rows,
                        "markdown": table.markdown,
                        "html": table.html,
                        "cells": table.cells,
                    }
                )
            for figure in section.figures:
                figure_payload.append(
                    {
                        "section_path": section.path,
                        "block_id": figure.block_id,
                        "caption": figure.caption,
                        "image": figure.image,
                        "page_num": figure.page_num,
                        "bbox": list(figure.bbox) if figure.bbox else None,
                    }
                )
        (directory / "parsed_tables.json").write_text(
            json.dumps(table_payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (directory / "parsed_figures.json").write_text(
            json.dumps(figure_payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        channel_counts: dict[str, int] = {}
        for hit in radar.hits:
            channel_counts[hit.channel] = channel_counts.get(hit.channel, 0) + 1
        parse_manifest = {
            "doc_id": str(doc_id),
            "markdown_backend": backend,
            "page_count": parsed.page_count,
            "section_count": len(scan_document.sections),
            "paragraph_count": sum(len(section.paragraphs) for section in scan_document.sections),
            "block_count": sum(len(section.paragraph_blocks) for section in scan_document.sections),
            "table_count": len(table_payload),
            "figure_count": len(figure_payload),
            "formula_count": sum(len(section.formulas) for section in scan_document.sections),
            "code_block_count": sum(len(section.code_blocks) for section in scan_document.sections),
            "radar_channels": channel_counts,
            "bundle_count": len(bundles),
            "bundles": [
                {
                    "section_path": bundle.section_path,
                    "page_num": bundle.page_num,
                    "channels": bundle.detected_channels,
                    "locator": bundle.locator,
                }
                for bundle in bundles
            ],
            "tables_file": "parsed_tables.json",
            "figures_file": "parsed_figures.json",
            "bundles_file": "bundles.json",
        }
        (directory / "parse_manifest.json").write_text(
            json.dumps(parse_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.db.update_parse_status(str(doc_id), "LIGHT_PARSED")
        mode = "手册/指南增强" if manual else "标准"
        self.db.log_event(
            "INFO",
            "parse",
            f"{doc_id} 解析完成（{mode}模式）：{len(parsed.sections)} 小节，"
            f"{len(bundles)} 个证据包",
            doc_id=str(doc_id),
        )
        self._log_onnx_status(doc_id)
        if progress:
            progress("证据包已生成", 0.55)
        return parsed, bundles, bundles_path

    def _log_onnx_status(self, doc_id: str) -> None:
        registry = getattr(self.gateway, "registry", None)
        if registry is None:
            return
        try:
            status = {item["name"]: item for item in registry.status()}
            mini = "激活" if status.get("mini_cluster_embed", {}).get("active") else "降级"
            nli = "激活" if status.get("conflict_nli", {}).get("active") else "降级"
            from parser.local_ocr import local_ocr_available

            ocr = "可用" if local_ocr_available() else "未安装（回退文本层）"
            self.db.log_event(
                "INFO",
                "parse",
                f"{doc_id} ONNX 全链路：MiniLM 语义{mini} · NLI 争议{nli} · "
                f"本地 OCR {ocr}",
                doc_id=str(doc_id),
            )
        except Exception:
            log.debug("ONNX status logging failed", exc_info=True)

    def handle_parse(self, task: Mapping[str, Any]) -> bool:
        doc_id = task.get("doc_id") or _payload(task).get("doc_id")
        if not doc_id:
            return False
        result = self.parse_document(str(doc_id))
        if result is None:
            return False
        _parsed, bundles, bundles_path = result
        if bundles:
            self.db.add_task(
                "EXTRACT",
                doc_id=str(doc_id),
                payload={"bundles_path": str(bundles_path.relative_to(self.workspace))},
                priority=8,
            )
        return True

    def _rank_bundles_onnx(
        self, bundles: list[ContextBundle], doc_id: str
    ) -> list[ContextBundle]:
        """Prioritise high-value manuscript sections with MiniLM, without dropping any."""
        if len(bundles) < 2:
            return bundles
        registry = getattr(self.gateway, "registry", None) or getattr(
            self.consensus, "registry", None
        )
        if registry is None:
            return bundles
        try:
            if not registry.is_present("mini_cluster_embed"):
                return bundles
            anchor = (
                "atomic physics calculation strategy, physical model, program workflow, "
                "input script, practical convergence rule, worked example, pitfall"
            )
            texts = [anchor] + [
                bundle.prompt_markdown()[:8000] for bundle in bundles
            ]
            vectors = registry.embed_minilm(texts)
            anchor_vec = vectors[0]
            def score(index: int) -> float:
                left, right = anchor_vec, vectors[index + 1]
                dot = sum(x * y for x, y in zip(left, right))
                na = sum(x * x for x in left) ** 0.5
                nb = sum(y * y for y in right) ** 0.5
                return dot / max(1e-9, na * nb)
            ordered = [
                bundle for _, bundle in sorted(
                    enumerate(bundles), key=lambda pair: score(pair[0]), reverse=True
                )
            ]
            self.db.log_event(
                "INFO",
                "extract",
                f"{doc_id} MiniLM 语义排序参与 {len(bundles)} 个候选证据包",
                doc_id=doc_id,
            )
            return ordered
        except Exception:
            log.debug("MiniLM bundle ranking failed; preserving parser order", exc_info=True)
            return bundles

    def extract_bundles(
        self,
        doc_id: str,
        bundles: list[ContextBundle],
        *,
        force: bool = False,
        progress: Any = None,
        should_stop: Any = None,
    ) -> dict[str, Any]:
        """Extract cards with bundle-hash caching and verified-card protection."""
        document = self.db.get_document(str(doc_id))
        if not document or self.gateway is None:
            return {
                "extracted": 0,
                "skipped_existing": 0,
                "failed": 0,
                "cards": 0,
                "cancelled": False,
            }
        if force:
            removed = self.db.delete_unverified_cards_for_document(str(doc_id))
            for knw_id in removed.get("deleted", []):
                if self.qdrant is not None:
                    try:
                        self.qdrant.delete(knw_id)
                    except Exception:
                        pass
            if removed.get("kept_verified"):
                self.db.log_event(
                    "INFO",
                    "extract",
                    f"{doc_id} 强制重提保留人工校准卡片 {len(removed['kept_verified'])} 张",
                    doc_id=str(doc_id),
                )

        manual_mode = is_manual_document(
            title=str(document.get("title") or ""),
            doc_type=str(document.get("doc_type") or ""),
            filename=Path(str(document.get("local_pdf_path") or "")).name,
        )
        skip_low_value = bool(
            _cfg(self.config, "extraction.skip_low_value_manual", True)
        )
        candidates: list[ContextBundle] = []
        skipped = 0
        skipped_low_value = 0
        for bundle in bundles:
            if not force and self.db.has_bundle_card(str(doc_id), bundle_hash(bundle)):
                skipped += 1
                continue
            if manual_mode and skip_low_value and is_low_value_bundle(
                bundle.section_path,
                bundle.focal_window_markdown,
                bundle.focal_sentence,
                manual=True,
            ):
                skipped_low_value += 1
                continue
            candidates.append(bundle)
        candidate_total = len(candidates)
        cancelled = bool(should_stop is not None and should_stop())
        if not cancelled:
            candidates = self._rank_bundles_onnx(candidates, str(doc_id))

        doc_metadata = {
            "doc_id": document["doc_id"],
            "title": document["title"],
            "authors": document.get("authors_list") or [],
            "journal": document.get("journal"),
            "year": document.get("pub_year"),
            "doc_type": document.get("doc_type") or "",
            "manual_mode": manual_mode,
        }

        def call_gateway(bundle: ContextBundle) -> Any:
            try:
                return self.gateway.extract_cards(
                    bundle, doc_metadata=doc_metadata, manual_mode=manual_mode
                )
            except TypeError:
                # Lightweight test doubles may not accept the new kwarg.
                return self.gateway.extract_cards(
                    bundle, doc_metadata=doc_metadata
                )

        extracted = 0
        failed = 0

        def consume(bundle: ContextBundle, result: Any = None, exc: Any = None) -> None:
            nonlocal extracted, failed
            if exc is not None:
                failed += 1
                log.warning(
                    "Extraction failed for bundle %s: %s",
                    bundle.section_path,
                    exc,
                )
                return
            self._apply_llm_metadata(document, result)
            if getattr(result, "skip", False):
                log.info(
                    "Skipping truncated evidence bundle %s: %s",
                    bundle.section_path,
                    getattr(result, "skip_reason", "") or "no reliable conclusion",
                )
                return
            for draft in result.cards:
                card_id = self._store_draft(draft, document, bundle, result.keywords)
                if card_id:
                    extracted += 1

        if hasattr(self.gateway, "set_active_document"):
            self.gateway.set_active_document(str(doc_id))
        try:
            concurrency = min(
                8,
                max(
                    1,
                    int(_cfg(self.config, "extraction.concurrency", 1) or 1),
                ),
            )
            if concurrency > 1 and len(candidates) > 1:
                with ThreadPoolExecutor(max_workers=concurrency) as executor:
                    futures: dict[Any, tuple[int, ContextBundle]] = {}
                    next_index = 0
                    processed = 0
                    while next_index < len(candidates) or futures:
                        if should_stop is not None and should_stop():
                            cancelled = True
                        while (
                            not cancelled
                            and len(futures) < concurrency
                            and next_index < len(candidates)
                        ):
                            if should_stop is not None and should_stop():
                                cancelled = True
                                break
                            bundle = candidates[next_index]
                            if progress:
                                progress(
                                    f"提交证据包 {next_index + 1}/{len(candidates)} · "
                                    f"{bundle.section_path}",
                                    0.55 + 0.4 * next_index / max(1, len(candidates)),
                                )
                            futures[executor.submit(call_gateway, bundle)] = (
                                next_index,
                                bundle,
                            )
                            next_index += 1
                        if not futures:
                            break
                        done, _ = wait(futures, return_when=FIRST_COMPLETED)
                        for future in done:
                            _index, bundle = futures.pop(future)
                            try:
                                result = future.result()
                                error = None
                            except Exception as exc:
                                result = None
                                error = exc
                            consume(bundle, result, error)
                            processed += 1
                            if progress:
                                progress(
                                    f"完成证据包 {processed}/{len(candidates)} · "
                                    f"{bundle.section_path}",
                                    0.55 + 0.4 * processed / max(1, len(candidates)),
                                )
                        if should_stop is not None and should_stop():
                            cancelled = True
            else:
                for index, bundle in enumerate(candidates):
                    if should_stop is not None and should_stop():
                        cancelled = True
                        break
                    if progress:
                        progress(
                            f"提炼证据包 {index + 1}/{len(candidates)} · {bundle.section_path}",
                            0.55 + 0.4 * (index + 1) / max(1, len(candidates)),
                        )
                    try:
                        result = call_gateway(bundle)
                        error = None
                    except Exception as exc:
                        result = None
                        error = exc
                    consume(bundle, result, error)
        finally:
            if hasattr(self.gateway, "set_active_document"):
                self.gateway.set_active_document(None)

        cards = len(self.db.cards_for_document(str(doc_id)))
        if cancelled:
            reason = (
                f"已取消增量补提：当前已关联 {cards} 张卡，"
                "已完成的部分已保存，可再次增量补提继续。"
            )
            self.db.update_parse_status(str(doc_id), "LIGHT_PARSED", reason)
            level = "WARNING"
        elif cards:
            self.db.update_parse_status(str(doc_id), "LLM_EXTRACTED")
            level = "INFO"
        else:
            if failed:
                reason = (
                    f"未生成知识卡：{failed} 个候选片段模型提取失败，"
                    "其余片段未产生有效内容。"
                )
            elif candidate_total:
                reason = (
                    "未生成知识卡：所有候选片段均被判定为证据截断或无有效原子物理内容。"
                    "若 PDF 是扫描件，请先 OCR 添加文本层。"
                )
            else:
                reason = "未生成知识卡：没有可用的候选证据片段。若 PDF 是扫描件，请先 OCR。"
            self.db.update_parse_status(str(doc_id), "FAILED", reason)
            level = "WARNING"
        self._log_onnx_status(str(doc_id))
        self.db.log_event(
            level,
            "extract",
            f"{doc_id} 提取 {extracted} 张知识卡；跳过已提炼片段 {skipped}；"
            f"过滤低价值片段 {skipped_low_value}；失败 {failed}；当前关联卡片 {cards}"
            + ("；用户已取消" if cancelled else ""),
            doc_id=str(doc_id),
        )
        return {
            "extracted": extracted,
            "skipped_existing": skipped,
            "skipped_low_value": skipped_low_value,
            "failed": failed,
            "cards": cards,
            "candidates": candidate_total,
            "cancelled": cancelled,
        }

    def handle_extract(self, task: Mapping[str, Any]) -> bool:
        doc_id = task.get("doc_id") or _payload(task).get("doc_id")
        if not doc_id:
            return False
        if self.gateway is None:
            self.db.log_event("ERROR", "extract", "模型网关未初始化，无法提取卡片", doc_id=str(doc_id))
            return False
        document = self.db.get_document(str(doc_id))
        if not document:
            return False
        payload = _payload(task)
        bundles_path = self._local_path(payload.get("bundles_path"))
        if bundles_path is None:
            pdf_path = self._local_path(document.get("local_pdf_path"))
            bundles_path = (pdf_path.parent / "bundles.json") if pdf_path else None
        if bundles_path is None or not bundles_path.exists():
            self.db.log_event("ERROR", "extract", "缺少 bundles.json", doc_id=str(doc_id))
            return False
        try:
            raw_bundles = json.loads(bundles_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            self.db.log_event("ERROR", "extract", f"bundles.json 损坏: {exc}", doc_id=str(doc_id))
            return False

        bundles: list[ContextBundle] = []
        for raw in raw_bundles:
            try:
                bundles.append(ContextBundle.model_validate(raw))
            except Exception:
                log.warning("Invalid bundle skipped for %s", doc_id, exc_info=True)
        result = self.extract_bundles(str(doc_id), bundles, force=False)
        return result["extracted"] > 0 or result["skipped_existing"] > 0

    def handle_consolidate(self, task: Mapping[str, Any]) -> bool:
        """Slow-brain adjudication of cards marked as conflicts."""
        conflicts = self.db.conflict_cards(limit=20)
        if not conflicts:
            return True
        if self.gateway is None:
            return True
        for card in conflicts:
            for other_id in card.get("conflict_with_knw_ids_list") or []:
                other = self.db.get_knowledge_card(other_id)
                if not other:
                    continue
                supporting = card.get("supporting_doc_ids_list") or []
                if hasattr(self.gateway, "set_active_document"):
                    self.gateway.set_active_document(supporting[0] if supporting else None)
                try:
                    verdict = self.gateway.adjudicate(card, other)
                except Exception:
                    log.exception("Conflict adjudication failed")
                    continue
                self.db.upsert_knowledge_card(
                    {
                        "knw_id": card["knw_id"],
                        "structured_details_json": {
                            **card.get("structured", {}),
                            "conflict_verdict": verdict.model_dump(),
                        },
                    }
                )
                if hasattr(self.gateway, "set_active_document"):
                    self.gateway.set_active_document(None)
                break
        return True

    def handle_report(self, task: Mapping[str, Any]) -> bool:
        from scheduler.morning_reporter import MorningReporter

        MorningReporter(self.db, self.workspace).generate()
        return True

    # --------------------------------------------------------------- helpers
    def revise_card(self, knw_id: str, instruction: str) -> dict[str, Any]:
        """Ask the fast model to revise one card from its L3 evidence, then save."""
        card = self.db.get_knowledge_card(knw_id)
        if not card:
            raise ValueError(f"找不到知识卡：{knw_id}")
        if self.gateway is None:
            raise RuntimeError("模型网关未配置")
        evidence_text = "\n\n".join(
            str(item.get("context_bundle_md") or "")
            for item in card.get("evidence") or []
        )
        doc_ids = card.get("supporting_doc_ids_list") or []
        active_doc = str(doc_ids[0]) if doc_ids else None
        if hasattr(self.gateway, "set_active_document"):
            self.gateway.set_active_document(active_doc)
        try:
            draft = self.gateway.revise_card(card, evidence_text, instruction)
        finally:
            if hasattr(self.gateway, "set_active_document"):
                self.gateway.set_active_document(None)

        def pick(value: Any, key: str) -> Any:
            return value if value not in (None, "") else card.get(key)

        updates: dict[str, Any] = {
            "knw_id": knw_id,
            "card_type": draft.card_type,
            "title_zh": draft.title_zh,
            "title_en": draft.title_en or card.get("title_en") or "",
            "summary_zh": draft.summary_zh,
            "premises_and_conditions": draft.premises_and_conditions,
            "element": pick(draft.element, "element"),
            "z_num": draft.z_num if draft.z_num is not None else card.get("z_num"),
            "ion_stage": pick(draft.ion_stage, "ion_stage"),
            "isoelectronic": pick(draft.isoelectronic, "isoelectronic"),
            "shell_type": pick(draft.shell_type, "shell_type"),
            "method": pick(draft.method, "method"),
            "code_name": pick(draft.code_name, "code_name"),
            "physical_process": pick(draft.physical_process, "physical_process"),
            "conclusion_flags": draft.conclusion_flags or card.get("conclusion_flags") or 0,
            "structured_details_json": _structured_with_conditions(draft) or card.get("structured_details") or {},
            "formulas_latex": draft.formulas_latex,
            "code_snippet": draft.code_snippet,
            "pitfall_warning": draft.pitfall_warning,
            "keywords": draft.keywords
            or [item.get("term_en") for item in card.get("keywords") or []],
            "is_vectorized": 0,
            "is_user_verified": 1,
        }
        updates, _notes = sanitize_card_fields(updates)
        if is_invalid_card(updates):
            raise ValueError("AI 返回了低质量/截断卡片，已拒绝保存")
        self.db.upsert_knowledge_card(updates)
        if self.gateway is not None:
            for role, info in collect_pool_provenance(
                getattr(self.gateway, "pool", None)
            ).items():
                self.db.set_card_provenance(
                    knw_id, role, info, source="revision"
                )
        self.reindex_card(knw_id)
        return self.db.get_knowledge_card(knw_id) or card

    def reindex_card(self, knw_id: str) -> None:
        """Refresh a card's embedding and Qdrant payload after an edit."""
        self.db.mark_vectorized([knw_id], False)
        card = self.db.get_knowledge_card(knw_id) or {}
        doc_ids = card.get("supporting_doc_ids_list") or []
        doc_id = str(doc_ids[0]) if doc_ids else None
        if self.qdrant is None or self.gateway is None:
            self._queue_vectorize(knw_id, doc_id)
            return
        if not getattr(self.qdrant, "available", False):
            self._queue_vectorize(knw_id, doc_id)
            return
        if not getattr(self.gateway.embedding, "available", False):
            try:
                self.qdrant.delete(knw_id)
            except Exception:
                pass
            self._queue_vectorize(knw_id, doc_id)
            return
        if hasattr(self.gateway, "set_active_document"):
            self.gateway.set_active_document(doc_id)
        indexed = False
        try:
            indexed = self._vectorize(knw_id)
        finally:
            if hasattr(self.gateway, "set_active_document"):
                self.gateway.set_active_document(None)
        if not indexed:
            try:
                self.qdrant.delete(knw_id)
            except Exception:
                pass
            self._queue_vectorize(knw_id, doc_id)

    def _apply_llm_metadata(self, document: Mapping[str, Any], result: Any) -> None:
        updates: dict[str, Any] = {}
        title = str(getattr(result, "document_title", "") or "").strip()
        if title and is_valid_title(title) and not is_valid_title(document.get("title")):
            updates["title"] = title
        journal = str(getattr(result, "document_journal", "") or "").strip()
        if journal and not document.get("journal"):
            updates["journal"] = journal
        year = getattr(result, "document_year", None)
        try:
            year = int(year) if year else None
        except (TypeError, ValueError):
            year = None
        if year and 1800 <= year <= 2200 and not document.get("pub_year"):
            updates["pub_year"] = year
        doi = str(getattr(result, "document_doi", "") or "").strip()
        if doi and not document.get("doi"):
            updates["doi"] = doi
        if updates:
            updates["doc_id"] = document["doc_id"]
            self.db.upsert_document(updates)
            if isinstance(document, dict):
                document.update(
                    {key: value for key, value in updates.items() if key != "doc_id"}
                )
            log.info("LLM metadata write-back for %s: %s", document["doc_id"], updates)

    def _local_path(self, value: Any) -> Path | None:
        if not value:
            return None
        path = Path(str(value))
        return path if path.is_absolute() else self.workspace / path

    def _store_draft(
        self,
        draft: KnowledgeCardDraft,
        document: Mapping[str, Any],
        bundle: ContextBundle,
        keywords: list[Any],
        *,
        provenance_override: Mapping[str, Mapping[str, Any]] | None = None,
        provenance_source: str = "extraction",
    ) -> str:
        card = {
            "card_level": "L2",
            "card_type": draft.card_type,
            "title_zh": draft.title_zh,
            "title_en": draft.title_en,
            "summary_zh": draft.summary_zh,
            "premises_and_conditions": draft.premises_and_conditions,
            "element": draft.element,
            "z_num": draft.z_num,
            "ion_stage": draft.ion_stage,
            "isoelectronic": draft.isoelectronic,
            "shell_type": draft.shell_type,
            "method": draft.method,
            "code_name": draft.code_name,
            "physical_process": draft.physical_process,
            "conclusion_flags": draft.conclusion_flags,
            "structured_details_json": _structured_with_conditions(draft),
            "formulas_latex": draft.formulas_latex,
            "code_snippet": draft.code_snippet,
            "pitfall_warning": draft.pitfall_warning,
            "supporting_doc_ids": draft.supporting_doc_ids or [document["doc_id"]],
            "source_section": draft.source_section or bundle.section_path,
            "page_num": draft.page_num or bundle.page_num,
            "recency_year": document.get("pub_year"),
            "card_year": document.get("pub_year"),
            "search_payload": draft.search_payload
            or build_search_payload(
                {
                    "card_type": draft.card_type,
                    "title_zh": draft.title_zh,
                    "summary_zh": draft.summary_zh,
                    "premises_and_conditions": draft.premises_and_conditions,
                    "ion_stage": draft.ion_stage,
                    "isoelectronic": draft.isoelectronic,
                    "z_num": draft.z_num,
                    "method": draft.method,
                    "code_name": draft.code_name,
                    "physical_process": draft.physical_process,
                    "keywords": draft.keywords,
                }
            ),
            "authority_score": document.get("authority_weight", 1.0),
            "consensus_count": 1,
        }
        card, _notes = sanitize_card_fields(card)
        if is_invalid_card(card):
            log.info("Dropping truncated/invalid card for %s", document["doc_id"])
            return ""

        existing = self.db.list_knowledge_cards(
            filters={
                "element": card.get("element"),
                "z_num": card.get("z_num"),
                "isoelectronic": card.get("isoelectronic"),
                "method": card.get("method"),
                "physical_process": card.get("physical_process"),
                "card_type": card.get("card_type"),
            },
            limit=30,
        )
        existing = [item for item in existing if item.get("knw_id") != card.get("knw_id")]
        consolidation = self.consensus.consolidate(card, existing)

        # V3.1: never discard the newly extracted card.  Similarity is a
        # candidate-recall signal, not proof that two physics claims are
        # interchangeable.  Keep both cards and record the relationship.
        card_id = self.db.upsert_knowledge_card(card)
        relation_evidence = {
            "doc_id": document["doc_id"],
            "bundle_hash": bundle_hash(bundle),
            "section_path": bundle.section_path,
        }
        if consolidation.duplicate and consolidation.target_id:
            target = self.db.get_knowledge_card(consolidation.target_id)
            if target:
                self.db.add_card_relation(
                    card_id,
                    target["knw_id"],
                    "SAME_CLAIM",
                    confidence=consolidation.similarity,
                    reason="；".join(consolidation.reasons),
                    evidence=relation_evidence,
                )
                self.db.upsert_knowledge_card(
                    {
                        "knw_id": target["knw_id"],
                        "consensus_count": consolidation.consensus_count,
                        "supporting_doc_ids": consolidation.supporting_doc_ids,
                        "has_conflict": 1
                        if consolidation.has_conflict
                        else target.get("has_conflict", 0),
                        "conflict_with_knw_ids": sorted(
                            set(target.get("conflict_with_knw_ids_list") or [])
                            | set(consolidation.conflict_ids)
                        ),
                    }
                )

        if consolidation.has_conflict:
            self.db.upsert_knowledge_card(
                {
                    "knw_id": card_id,
                    "has_conflict": 1,
                    "conflict_with_knw_ids": consolidation.conflict_ids,
                }
            )
            for other_id in consolidation.conflict_ids:
                other = self.db.get_knowledge_card(other_id)
                if not other:
                    continue
                self.db.add_card_relation(
                    card_id,
                    other_id,
                    "CONTRADICTS",
                    confidence=0.6,
                    reason="；".join(consolidation.reasons),
                    evidence=relation_evidence,
                )
                self.db.upsert_knowledge_card(
                    {
                        "knw_id": other_id,
                        "has_conflict": 1,
                        "conflict_with_knw_ids": sorted(
                            set(other.get("conflict_with_knw_ids_list") or [])
                            | {card_id}
                        ),
                    }
                )

        # L3 evidence belongs to the newly stored card.  The relation table
        # links it to its canonical/similar sibling without hiding either one.
        locator = dict(bundle.locator or {})
        locator.update(
            {
                "doc_id": document["doc_id"],
                "pdf_sha256": document.get("pdf_sha256") or "",
                "markdown_backend": str(
                    ((document.get("metadata") or {}).get("markdown_backend") or "")
                ),
            }
        )
        self.db.add_evidence(
            card_id,
            document["doc_id"],
            bundle.prompt_markdown(),
            section_title=bundle.section_path,
            page_num=bundle.page_num,
            bundle_hash=bundle_hash(bundle),
            locator=locator,
        )
        keyword_payload: list[Any] = []
        for keyword in keywords:
            if hasattr(keyword, "model_dump"):
                keyword_payload.append(keyword.model_dump())
            elif isinstance(keyword, Mapping):
                keyword_payload.append(dict(keyword))
        for term in draft.keywords:
            keyword_payload.append({"term_en": term})
        if keyword_payload:
            self.db.link_card_keywords(card_id, keyword_payload)
        if self.gateway is not None:
            provenance = dict(
                collect_pool_provenance(getattr(self.gateway, "pool", None))
            )
            if provenance_override:
                for role, info in provenance_override.items():
                    if isinstance(info, Mapping) and info:
                        provenance[str(role)] = dict(info)
            for role, info in provenance.items():
                self.db.set_card_provenance(
                    card_id, role, info, source=provenance_source
                )
        elif provenance_override:
            for role, info in provenance_override.items():
                if isinstance(info, Mapping) and info:
                    self.db.set_card_provenance(
                        card_id, str(role), dict(info), source=provenance_source
                    )
        if not self._vectorize(card_id):
            self._queue_vectorize(card_id, document["doc_id"])
        return card_id

    def _queue_vectorize(
        self,
        knw_id: str,
        doc_id: str | None = None,
        *,
        delay_sec: int = 0,
    ) -> None:
        try:
            self.db.enqueue_vector_tasks(
                [knw_id], doc_id=doc_id, delay_sec=delay_sec
            )
        except Exception:
            log.exception("Could not queue vector refresh for %s", knw_id)

    def handle_vectorize(self, task: Mapping[str, Any]) -> bool:
        """Retryable Qdrant refresh; SQLite remains the source of truth."""
        payload = _payload(task)
        knw_id = str(payload.get("knw_id") or task.get("doc_id") or "").strip()
        if not knw_id:
            return False
        if self._vectorize(knw_id):
            return True
        attempts = int(task.get("attempts") or 1)
        if attempts < 5:
            delay = min(300, 10 * (2 ** (attempts - 1)))
            self._queue_vectorize(
                knw_id, task.get("doc_id"), delay_sec=delay
            )
            self.db.log_event(
                "WARNING",
                "vector",
                f"{knw_id} 向量更新失败，第 {attempts} 次；{delay}s 后重试",
            )
        else:
            self.db.log_event(
                "ERROR",
                "vector",
                f"{knw_id} 向量更新连续失败 {attempts} 次，保留 needs_vector_update=1",
            )
        return True

    def _vectorize(self, knw_id: str) -> bool:
        if self.qdrant is None or self.gateway is None:
            return False
        if not getattr(self.qdrant, "available", False):
            return False
        if not getattr(self.gateway.embedding, "available", False):
            return False
        card = self.db.get_knowledge_card(knw_id)
        if not card:
            return False
        try:
            vector = self.gateway.embedding.embed_query(card["search_payload"])
            self.qdrant.upsert_card(
                knw_id,
                vector,
                {
                    "card_level": card.get("card_level"),
                    "card_type": card.get("card_type"),
                    "element": card.get("element"),
                    "z_num": card.get("z_num"),
                    "isoelectronic": card.get("isoelectronic"),
                    "method": card.get("method"),
                    "physical_process": card.get("physical_process"),
                    "code_name": card.get("code_name"),
                    "has_conflict": card.get("has_conflict", 0),
                    "conclusion_flags": card.get("conclusion_flags", 0),
                },
            )
            self.db.mark_vectorized([knw_id], True)
            return True
        except Exception:
            log.exception("Vectorization failed for %s", knw_id)
            return False


def _payload(task: Mapping[str, Any]) -> dict[str, Any]:
    value = task.get("payload_json")
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            data = json.loads(value)
            return dict(data) if isinstance(data, dict) else {}
        except ValueError:
            return {}
    return {}
