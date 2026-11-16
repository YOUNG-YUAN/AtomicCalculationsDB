"""Background QThread workers so network / parsing never blocks the GUI."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterable

from PySide6.QtCore import QThread, Signal

log = logging.getLogger(__name__)


class SearchWorker(QThread):
    finished_hits = Signal(object)
    finished_page = Signal(object, int)
    failed = Signal(str)

    def __init__(
        self,
        retriever: Any,
        query: str,
        parent=None,
        extra_filters=None,
        *,
        page: int = 1,
        page_size: int = 20,
    ):
        super().__init__(parent)
        self.retriever = retriever
        self.query = query
        self.extra_filters = dict(extra_filters or {})
        self.page = max(1, int(page))
        self.page_size = max(1, min(int(page_size), 200))

    def run(self) -> None:
        try:
            if hasattr(self.retriever, "search_page"):
                hits, total = self.retriever.search_page(
                    self.query,
                    page=self.page,
                    page_size=self.page_size,
                    extra_filters=self.extra_filters,
                )
            else:
                hits = self.retriever.search(
                    self.query,
                    limit=self.page_size,
                    extra_filters=self.extra_filters,
                    include_evidence=False,
                )
                total = len(hits)
            self.finished_hits.emit(hits)
            self.finished_page.emit(hits, int(total))
        except Exception as exc:
            log.exception("Search worker failed")
            self.failed.emit(str(exc))


class IngestWorker(QThread):
    finished_results = Signal(object)
    failed = Signal(str)

    def __init__(self, watcher: Any, paths: Iterable[str], parent=None):
        super().__init__(parent)
        self.watcher = watcher
        self.paths = list(paths)

    def run(self) -> None:
        try:
            results = self.watcher.ingest_paths(self.paths)
            self.finished_results.emit(results)
        except Exception as exc:
            log.exception("Ingest worker failed")
            self.failed.emit(str(exc))


class DiscoveryWorker(QThread):
    finished_results = Signal(object)
    failed = Signal(str)

    def __init__(self, fetcher: Any, query: str, sources: Iterable[str], parent=None):
        super().__init__(parent)
        self.fetcher = fetcher
        self.query = query
        self.sources = list(sources)

    def run(self) -> None:
        try:
            ids = self.fetcher.discover_and_queue(self.query, sources=self.sources)
            self.finished_results.emit(ids)
        except Exception as exc:
            log.exception("Discovery worker failed")
            self.failed.emit(str(exc))


class PackageWorker(QThread):
    finished_result = Signal(object)
    failed = Signal(str)

    def __init__(self, action: str, packager: Any, target: str, parent=None):
        super().__init__(parent)
        self.action = action
        self.packager = packager
        self.target = target

    def run(self) -> None:
        try:
            if self.action == "export":
                self.finished_result.emit(self.packager.export(self.target))
            else:
                self.finished_result.emit(self.packager.import_package(self.target))
        except Exception as exc:
            log.exception("Package worker failed")
            self.failed.emit(str(exc))


class SyncWorker(QThread):
    finished_result = Signal(object)
    failed = Signal(str)

    def __init__(self, sync_manager: Any, provider: str, parent=None):
        super().__init__(parent)
        self.sync_manager = sync_manager
        self.provider = provider

    def run(self) -> None:
        try:
            if self.provider == "webdav":
                result = self.sync_manager.sync_webdav()
            elif self.provider == "s3":
                result = self.sync_manager.sync_s3()
            else:
                result = self.sync_manager.sync_webdav()
            self.finished_result.emit(result)
        except Exception as exc:
            log.exception("Sync worker failed")
            self.failed.emit(str(exc))


class ScanInboxWorker(QThread):
    finished_results = Signal(object)
    failed = Signal(str)

    def __init__(self, watcher: Any, parent=None):
        super().__init__(parent)
        self.watcher = watcher

    def run(self) -> None:
        try:
            self.finished_results.emit(self.watcher.scan())
        except Exception as exc:
            log.exception("Inbox scan failed")
            self.failed.emit(str(exc))


class ModelDownloadWorker(QThread):
    progress_event = Signal(dict)
    finished_download = Signal(dict)
    failed = Signal(str)

    def __init__(self, registry: Any, *, force: bool = True, parent=None):
        super().__init__(parent)
        self.registry = registry
        self.force = force

    def run(self) -> None:
        try:
            result = self.registry.download_all(
                progress=self.progress_event.emit, force=self.force
            )
            self.finished_download.emit(result)
        except Exception as exc:
            log.exception("Model download worker failed")
            self.failed.emit(str(exc))


class MirrorProbeWorker(QThread):
    finished_results = Signal(object)
    failed = Signal(str)

    def __init__(self, mirrors: Iterable[str], parent=None):
        super().__init__(parent)
        self.mirrors = list(mirrors)

    def run(self) -> None:
        try:
            from crawler.mirror_probe import probe_mirrors

            self.finished_results.emit(probe_mirrors(self.mirrors))
        except Exception as exc:
            log.exception("Mirror probe failed")
            self.failed.emit(str(exc))


class ReprocessWorker(QThread):
    progress_event = Signal(str, float)
    finished_result = Signal(dict)
    failed = Signal(str)

    def __init__(
        self,
        manager: Any,
        mode: str,
        doc_ids: Iterable[str] | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.manager = manager
        self.mode = mode
        self.doc_ids = list(doc_ids or [])

    def cancel(self) -> None:
        """Request a cooperative stop; the current API call is allowed to finish."""
        self.requestInterruption()

    def run(self) -> None:
        try:
            progress = self.progress_event.emit
            if self.mode == "local":
                result = self.manager.local_clean(progress=progress)
            elif self.mode == "incremental":
                result = self.manager.incremental(
                    self.doc_ids or None,
                    progress=progress,
                    should_stop=self.isInterruptionRequested,
                )
            elif self.mode == "force":
                result = self.manager.force(
                    self.doc_ids,
                    progress=progress,
                    should_stop=self.isInterruptionRequested,
                )
            else:
                raise ValueError(f"未知重处理模式: {self.mode}")
            self.finished_result.emit(result)
        except Exception as exc:
            log.exception("Reprocess worker failed")
            self.failed.emit(str(exc))


class CardRevisionWorker(QThread):
    finished_card = Signal(object)
    failed = Signal(str)

    def __init__(self, pipeline: Any, knw_id: str, instruction: str, parent=None):
        super().__init__(parent)
        self.pipeline = pipeline
        self.knw_id = knw_id
        self.instruction = instruction

    def run(self) -> None:
        try:
            card = self.pipeline.revise_card(self.knw_id, self.instruction)
            self.finished_card.emit(card)
        except Exception as exc:
            log.exception("AI card revision failed")
            self.failed.emit(str(exc))


class MetadataRepairWorker(QThread):
    progress_event = Signal(int, int, str)
    finished_result = Signal(dict)
    failed = Signal(str)

    def __init__(self, db: Any, workspace: str, parent=None):
        super().__init__(parent)
        self.db = db
        self.workspace = workspace

    def run(self) -> None:
        try:
            from parser.metadata_extractor import repair_invalid_documents

            result = repair_invalid_documents(
                self.db,
                self.workspace,
                progress=self.progress_event.emit,
            )
            self.finished_result.emit(result)
        except Exception as exc:
            log.exception("Metadata repair worker failed")
            self.failed.emit(str(exc))


class ParseWorker(QThread):
    """Parse/OCR only, used before generating semi-automatic task packages."""

    progress_event = Signal(str, float)
    finished_result = Signal(object)
    failed = Signal(str)

    def __init__(self, pipeline: Any, doc_ids: Iterable[str], parent=None):
        super().__init__(parent)
        self.pipeline = pipeline
        self.doc_ids = list(doc_ids or [])

    def run(self) -> None:
        try:
            from models.semi_auto import parse_documents

            self.finished_result.emit(
                parse_documents(
                    self.pipeline,
                    self.doc_ids,
                    progress=self.progress_event.emit,
                )
            )
        except Exception as exc:
            log.exception("Parse worker failed")
            self.failed.emit(str(exc))
