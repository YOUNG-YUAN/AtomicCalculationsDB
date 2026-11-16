"""Manual PDF drop / inbox watcher.

When a publisher download fails, the document enters ``MANUAL_REQUIRED``.  A
user-downloaded PDF can be dragged into the GUI or placed in
``workspace/inbox_manual``; this module extracts the first-page DOI/title,
matches the queued document, moves the file into ``storage/{DOC-ID}``, attaches
it and queues the parse task.
"""

from __future__ import annotations

import difflib
import logging
import re
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from crawler.waterfall_fetcher import sha256_file
from db.id_generator import normalize_doi, normalize_title
from db.sqlite_manager import SQLiteManager
from parser.metadata_extractor import is_valid_title
from parser.pdf_to_markdown import extract_pdf_identity

log = logging.getLogger(__name__)


@dataclass
class IngestResult:
    doc_id: str
    created: bool
    matched: bool
    pdf_path: str = ""
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "created": self.created,
            "matched": self.matched,
            "pdf_path": self.pdf_path,
            "error": self.error,
        }


def _title_key(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


class ManualInboxWatcher:
    def __init__(self, db: SQLiteManager, workspace_dir: str | Path):
        self.db = db
        self.workspace = Path(workspace_dir)
        self.inbox = self.workspace / "inbox_manual"
        self.storage = self.workspace / "storage"

    def scan(self) -> list[IngestResult]:
        self.inbox.mkdir(parents=True, exist_ok=True)
        results = []
        for pdf in sorted(self.inbox.glob("*.pdf")):
            results.append(self._ingest_one(pdf, move=True))
        return results

    def ingest_paths(self, paths: Iterable[str | Path]) -> list[IngestResult]:
        return [self._ingest_one(Path(path), move=False) for path in paths]

    # ------------------------------------------------------------------ core
    def _ingest_one(self, source: Path, *, move: bool) -> IngestResult:
        try:
            if not source.is_file():
                return IngestResult("", False, False, error=f"文件不存在: {source}")
            if source.suffix.lower() != ".pdf":
                return IngestResult("", False, False, error=f"只支持 PDF: {source.name}")
            digest = sha256_file(source)
            existing = self.db.find_document_by_pdf_hash(digest)
            if existing:
                return IngestResult(
                    existing["doc_id"], False, True, existing.get("local_pdf_path") or ""
                )

            identity = extract_pdf_identity(source)
            doi = normalize_doi(identity.get("doi")) or ""
            title = " ".join(str(identity.get("title") or "").split())
            if not is_valid_title(title):
                title = re.sub(r"[_]+", " ", source.stem).strip()
            document = self._match_document(doi, title)
            created = document is None
            if document is None:
                doc_id = self.db.upsert_document(
                    {
                        "doc_type": "PAP",
                        "title": title,
                        "doi": doi or None,
                        "journal": identity.get("journal") or "",
                        "pub_year": identity.get("year"),
                        "authors": identity.get("authors") or [],
                        "source_url": f"https://doi.org/{doi}" if doi else "",
                        "download_status": "DOWNLOADED",
                        "parse_status": "PENDING",
                        "metadata_json": {
                            "manual_ingest": {
                                "original_name": source.name,
                                "probe_doi": doi,
                            }
                        },
                    }
                )
            else:
                doc_id = document["doc_id"]
                updates: dict[str, Any] = {"doc_id": doc_id}
                if is_valid_title(title) and not is_valid_title(document.get("title")):
                    updates["title"] = title
                if identity.get("journal") and not document.get("journal"):
                    updates["journal"] = identity["journal"]
                if identity.get("year") and not document.get("pub_year"):
                    updates["pub_year"] = identity["year"]
                if doi and not document.get("doi"):
                    updates["doi"] = doi
                if identity.get("authors") and not document.get("authors_list"):
                    updates["authors"] = identity["authors"]
                if len(updates) > 1:
                    self.db.upsert_document(updates)

            target = self._move_into_storage(doc_id, source, digest, move=move)
            self.db.attach_pdf(
                doc_id,
                target,
                sha256=digest,
                source_url=f"https://doi.org/{doi}" if doi else None,
            )
            # Preserve curated metadata; add the manual-ingest note.
            self.db.upsert_document(
                {
                    "doc_id": doc_id,
                    "metadata_json": {
                        "manual_ingest": {
                            "original_name": source.name,
                            "probe_doi": doi,
                            "probe_title": title,
                        }
                    },
                }
            )
            self.db.log_event(
                "INFO",
                "manual_ingest",
                f"手动 PDF 入库 {doc_id}: {source.name}",
                doc_id=doc_id,
            )
            return IngestResult(
                doc_id, created, not created, str(target)
            )
        except Exception as exc:
            log.exception("Manual ingest failed for %s", source)
            return IngestResult("", False, False, error=str(exc))

    def _match_document(self, doi: str, title: str) -> dict[str, Any] | None:
        if doi:
            document = self.db.find_document_by_doi(doi)
            if document:
                return document
        wanted = _title_key(title)
        if not wanted:
            return None
        documents = self.db.list_documents(limit=2000)
        best: tuple[float, dict[str, Any]] | None = None
        for document in documents:
            candidate = _title_key(document.get("title"))
            if not candidate:
                continue
            ratio = difflib.SequenceMatcher(None, wanted, candidate).ratio()
            if ratio >= 0.92 and (best is None or ratio > best[0]):
                best = (ratio, document)
        return best[1] if best else None

    def _move_into_storage(
        self, doc_id: str, source: Path, digest: str, *, move: bool
    ) -> Path:
        directory = self.storage / doc_id
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / source.name
        if target.exists() and target.resolve() != source.resolve():
            if sha256_file(target) == digest:
                return target
            target = directory / f"{source.stem}_{digest[:8]}{source.suffix}"
        if move:
            shutil.move(str(source), target)
        else:
            shutil.copy2(source, target)
        return target

    # -------------------------------------------------------------- polling
    def start_polling(
        self,
        *,
        interval: float = 30.0,
        stop_event: threading.Event | None = None,
    ) -> threading.Thread:
        stop_event = stop_event or threading.Event()

        def loop() -> None:
            while not stop_event.is_set():
                try:
                    self.scan()
                except Exception:
                    log.exception("Inbox scan failed")
                stop_event.wait(interval)

        thread = threading.Thread(target=loop, name="acdb-inbox-watcher", daemon=True)
        thread.start()
        return thread
