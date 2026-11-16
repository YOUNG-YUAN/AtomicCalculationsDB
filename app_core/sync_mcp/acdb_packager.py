"""``.acdb`` knowledge-pack export / incremental merge import.

A pack is a ZIP containing:

* ``manifest.json``       format/version/counts/embedding signature
* ``meta_dump.jsonl``     documents, cards, evidence, keywords, links, notation
* ``vectors.jsonl``       optional Qdrant vectors (JSONL fallback when pyarrow
                          is absent; ``manifest.vectors_format`` records this)
* ``pdfs/{DOC-ID}/...``   optional source PDFs

Import is ID-based: same ``DOC-/KNW-/KW-`` IDs merge, new IDs are inserted.
No deletion is ever performed by an import.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping

from db.sqlite_manager import SQLiteManager

log = logging.getLogger(__name__)

FORMAT = "acdb/2.0"


def _app_version() -> str:
    try:
        path = Path(__file__).resolve().parents[1] / "version.json"
        return str(json.loads(path.read_text(encoding="utf-8")).get("version") or "2.0.0")
    except Exception:
        return "2.0.0"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class ACDBPackager:
    def __init__(
        self,
        db: SQLiteManager,
        workspace_dir: str | Path,
        *,
        qdrant: Any = None,
    ):
        self.db = db
        self.workspace = Path(workspace_dir)
        self.qdrant = qdrant

    # ---------------------------------------------------------------- export
    def export(
        self,
        target: str | Path,
        *,
        doc_ids: Iterable[str] | None = None,
        knw_ids: Iterable[str] | None = None,
        include_pdfs: bool = True,
    ) -> Path:
        target = Path(target)
        if target.suffix.lower() != ".acdb":
            target = target.with_suffix(".acdb")
        target.parent.mkdir(parents=True, exist_ok=True)

        if doc_ids:
            documents = [
                doc
                for doc_id in dict.fromkeys(str(item).upper() for item in doc_ids)
                if (doc := self.db.get_document(doc_id))
            ]
        elif knw_ids:
            cards = self.db.get_knowledge_cards(list(knw_ids))
            support = {
                doc_id
                for card in cards
                for doc_id in (card.get("supporting_doc_ids_list") or [])
            }
            documents = [
                doc
                for doc_id in sorted(support)
                if (doc := self.db.get_document(doc_id))
            ]
        else:
            documents = self.db.list_documents(limit=2000)

        doc_set = {doc["doc_id"] for doc in documents}
        if knw_ids:
            cards = self.db.get_knowledge_cards(list(knw_ids))
        elif doc_ids:
            cards = [
                card
                for card in self.db.list_knowledge_cards(limit=2000)
                if set(card.get("supporting_doc_ids_list") or []) & doc_set
            ]
        else:
            cards = self.db.list_knowledge_cards(limit=2000)
        card_set = {card["knw_id"] for card in cards}

        evidence = [
            item
            for card in cards
            for item in self.db.list_evidence(card["knw_id"])
        ]
        keywords = self.db.list_keywords(limit=2000)
        links = [
            link
            for link in self.db.all_card_keyword_links()
            if link["knw_id"] in card_set
        ]
        notations = self.db.list_notations(limit=2000)

        manifest = {
            "format": FORMAT,
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "app_version": _app_version(),
            "embedding_signature": self.db.embedding_signature(),
            "filters": {
                "doc_ids": sorted(doc_set) if doc_ids else [],
                "knw_ids": sorted(card_set) if knw_ids else [],
            },
            "include_pdfs": bool(include_pdfs),
            "counts": {
                "documents": len(documents),
                "cards": len(cards),
                "evidence": len(evidence),
                "keywords": len(keywords),
                "card_keywords": len(links),
                "notations": len(notations),
            },
            "vectors_format": "jsonl",
            "pdf_files": [],
        }

        pdf_entries: list[tuple[str, Path]] = []
        if include_pdfs:
            for doc in documents:
                value = doc.get("local_pdf_path")
                if not value:
                    continue
                source = Path(value)
                if not source.is_absolute():
                    source = self.workspace / source
                if not source.is_file():
                    continue
                arcname = f"pdfs/{doc['doc_id']}/{source.name}"
                pdf_entries.append((arcname, source))
                manifest["pdf_files"].append(
                    {"doc_id": doc["doc_id"], "path": arcname, "sha256": _sha256_file(source)}
                )

        vector_records: list[dict[str, Any]] = []
        if self.qdrant is not None and getattr(self.qdrant, "available", False):
            for record in self.qdrant.export_all(limit=20000):
                if record.get("knw_id") in card_set and record.get("vector"):
                    vector_records.append(record)

        vectors_format = "jsonl"
        parquet_bytes: bytes | None = None
        if vector_records:
            try:
                import pyarrow as pa
                import pyarrow.parquet as pq

                table = pa.Table.from_pylist(
                    [
                        {
                            "knw_id": item["knw_id"],
                            "vector": list(item["vector"]),
                            "payload": json.dumps(
                                item.get("payload") or {}, ensure_ascii=False
                            ),
                        }
                        for item in vector_records
                    ]
                )
                sink = pa.BufferOutputStream()
                pq.write_table(table, sink)
                parquet_bytes = sink.getvalue().to_pybytes()
                vectors_format = "parquet"
            except Exception:
                vectors_format = "jsonl"
        manifest["vectors_format"] = vectors_format

        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            )
            meta_lines: list[str] = []
            for doc in documents:
                meta_lines.append(
                    json.dumps(
                        {"type": "document", "data": self._strip_runtime(doc)},
                        ensure_ascii=False,
                    )
                )
            for card in cards:
                meta_lines.append(
                    json.dumps(
                        {"type": "knowledge_card", "data": self._strip_runtime(card)},
                        ensure_ascii=False,
                    )
                )
            for item in evidence:
                meta_lines.append(
                    json.dumps({"type": "evidence", "data": item}, ensure_ascii=False)
                )
            for keyword in keywords:
                meta_lines.append(
                    json.dumps({"type": "keyword", "data": keyword}, ensure_ascii=False)
                )
            for link in links:
                meta_lines.append(
                    json.dumps({"type": "card_keyword", "data": link}, ensure_ascii=False)
                )
            for notation in notations:
                meta_lines.append(
                    json.dumps({"type": "notation", "data": notation}, ensure_ascii=False)
                )
            archive.writestr("meta_dump.jsonl", "\n".join(meta_lines) + "\n")
            if parquet_bytes is not None:
                archive.writestr("vectors.parquet", parquet_bytes)
            else:
                archive.writestr(
                    "vectors.jsonl",
                    "\n".join(
                        json.dumps(item, ensure_ascii=False) for item in vector_records
                    )
                    + ("\n" if vector_records else ""),
                )
            for arcname, source in pdf_entries:
                archive.write(source, arcname)

        self.db.log_event("INFO", "acdb", f"导出知识包 {target.name}")
        log.info("Exported %s (%s documents, %s cards)", target, len(documents), len(cards))
        return target

    @staticmethod
    def _strip_runtime(item: Mapping[str, Any]) -> dict[str, Any]:
        data = dict(item)
        for key in (
            "authors_list",
            "keywords_list",
            "metadata",
            "structured",
            "supporting_doc_ids_list",
            "conflict_with_knw_ids_list",
            "synonyms_list",
            "snippet",
            "bm25_score",
            "vector_score",
        ):
            data.pop(key, None)
        return data

    # -------------------------------------------------------- snapshot merge
    def merge_snapshot(
        self,
        snapshot: str | Path,
        *,
        vectorizer: Callable[[Mapping[str, Any]], list[float] | None] | None = None,
    ) -> dict[str, int]:
        """Incrementally merge a cloud SQLite snapshot into the live database.

        New IDs are inserted.  Existing documents are refreshed when the cloud
        ``updated_at`` is newer; cards are refreshed when either their
        ``updated_at`` is newer or their ``consensus_count`` is higher.  Nothing
        is ever deleted.  Re-vectorisation uses the caller's live embedder so
        the Qdrant index stays consistent without copying vector files.
        """
        path = Path(snapshot)
        if not path.is_file():
            raise FileNotFoundError(path)
        counts = {
            "documents_added": 0,
            "documents_updated": 0,
            "cards_added": 0,
            "cards_updated": 0,
            "evidence_added": 0,
            "keywords_added": 0,
            "card_keywords_added": 0,
            "notations_added": 0,
            "vectors_updated": 0,
        }
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            tables = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            vector_ids: set[str] = set()
            if "documents" in tables:
                for row in conn.execute("SELECT * FROM documents").fetchall():
                    data = dict(row)
                    doc_id = str(data.get("doc_id") or "").upper()
                    if not doc_id:
                        continue
                    existing = self.db.get_document(doc_id)
                    if existing is None:
                        self.db.upsert_document(data)
                        counts["documents_added"] += 1
                    elif str(data.get("updated_at") or "") > str(existing.get("updated_at") or ""):
                        self.db.upsert_document(data)
                        counts["documents_updated"] += 1
            if "knowledge_cards" in tables:
                for row in conn.execute("SELECT * FROM knowledge_cards").fetchall():
                    data = dict(row)
                    knw_id = str(data.get("knw_id") or "").upper()
                    if not knw_id:
                        continue
                    existing = self.db.get_knowledge_card(knw_id)
                    if existing is None:
                        data["is_vectorized"] = 0
                        self.db.upsert_knowledge_card(data)
                        counts["cards_added"] += 1
                        vector_ids.add(knw_id)
                    elif (
                        str(data.get("updated_at") or "") > str(existing.get("updated_at") or "")
                        or int(data.get("consensus_count") or 0)
                        > int(existing.get("consensus_count") or 0)
                    ):
                        data["is_vectorized"] = 0
                        self.db.upsert_knowledge_card(data)
                        counts["cards_updated"] += 1
                        vector_ids.add(knw_id)
            if "raw_evidence_chunks" in tables:
                for row in conn.execute("SELECT * FROM raw_evidence_chunks").fetchall():
                    data = dict(row)
                    chunk_id = str(data.get("chunk_id") or "")
                    if not chunk_id or self.db.get_evidence(chunk_id):
                        continue
                    try:
                        self.db.add_evidence(
                            data.get("parent_knw_id", ""),
                            data.get("doc_id", ""),
                            data.get("context_bundle_md", ""),
                            section_title=data.get("section_title", ""),
                            page_num=data.get("page_num"),
                            chunk_id=chunk_id,
                        )
                        counts["evidence_added"] += 1
                    except Exception:
                        log.exception("Snapshot evidence merge skipped")
            if "controlled_keywords" in tables:
                for row in conn.execute("SELECT * FROM controlled_keywords").fetchall():
                    data = dict(row)
                    if not data.get("term_en"):
                        continue
                    with self.db._lock:
                        existed = self.db.conn.execute(
                            "SELECT 1 FROM controlled_keywords WHERE kw_id=?",
                            (str(data.get("kw_id") or "").upper(),),
                        ).fetchone()
                    self.db.upsert_keyword(
                        data.get("term_en", ""),
                        term_zh=data.get("term_zh", ""),
                        synonyms=data.get("synonyms_json") or [],
                        category=data.get("category", ""),
                        kw_id=data.get("kw_id"),
                    )
                    if not existed:
                        counts["keywords_added"] += 1
            if "card_keywords" in tables:
                for row in conn.execute("SELECT * FROM card_keywords").fetchall():
                    data = dict(row)
                    if not data.get("knw_id") or not data.get("kw_id"):
                        continue
                    with self.db._lock:
                        exists = self.db.conn.execute(
                            "SELECT 1 FROM card_keywords WHERE knw_id=? AND kw_id=?",
                            (data["knw_id"], data["kw_id"]),
                        ).fetchone()
                    if exists:
                        continue
                    try:
                        self.db.link_card_keywords(
                            data["knw_id"],
                            [data["kw_id"]],
                            weight=float(data.get("weight") or 1.0),
                        )
                        counts["card_keywords_added"] += 1
                    except Exception:
                        log.exception("Snapshot keyword link merge skipped")
            if "notation_map" in tables:
                for row in conn.execute("SELECT * FROM notation_map").fetchall():
                    data = dict(row)
                    if not data.get("raw_symbol"):
                        continue
                    self.db.upsert_notation(
                        data.get("raw_symbol", ""),
                        data.get("canonical_symbol", ""),
                        data.get("category", ""),
                    )
                    counts["notations_added"] += 1

            for knw_id in sorted(vector_ids):
                if self._reindex_card(knw_id, vectorizer):
                    counts["vectors_updated"] += 1
        finally:
            conn.close()
        self.db.log_event("INFO", "sync", f"云端快照热合并完成: {counts}")
        log.info("Merged snapshot %s: %s", path, counts)
        return counts

    def _reindex_card(
        self,
        knw_id: str,
        vectorizer: Callable[[Mapping[str, Any]], list[float] | None] | None,
    ) -> bool:
        if (
            vectorizer is None
            or self.qdrant is None
            or not getattr(self.qdrant, "available", False)
        ):
            return False
        card = self.db.get_knowledge_card(knw_id)
        if not card:
            return False
        try:
            vector = vectorizer(card)
            if not vector:
                return False
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
            log.exception("Snapshot re-vectorisation failed for %s", knw_id)
            return False

    # ---------------------------------------------------------------- import
    def import_package(self, package: str | Path) -> dict[str, Any]:
        package = Path(package)
        if not package.is_file():
            raise FileNotFoundError(package)
        if not zipfile.is_zipfile(package):
            raise ValueError("不是有效的 .acdb 压缩包")
        counts = {
            "documents": 0,
            "cards": 0,
            "evidence": 0,
            "keywords": 0,
            "card_keywords": 0,
            "notations": 0,
            "vectors": 0,
        }
        with zipfile.ZipFile(package) as archive:
            names = set(archive.namelist())
            if "manifest.json" not in names or "meta_dump.jsonl" not in names:
                raise ValueError("知识包缺少 manifest.json 或 meta_dump.jsonl")
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            if not str(manifest.get("format", "")).startswith("acdb/"):
                raise ValueError("不支持的 .acdb 格式")

            pdf_hashes: dict[str, str] = {}
            for entry in manifest.get("pdf_files") or []:
                doc_id = str(entry.get("doc_id") or "")
                arcname = str(entry.get("path") or "")
                if not doc_id or arcname not in names:
                    continue
                pure = PurePosixPath(arcname)
                if pure.is_absolute() or ".." in pure.parts:
                    continue
                directory = self.workspace / "storage" / doc_id
                directory.mkdir(parents=True, exist_ok=True)
                target = directory / PurePosixPath(arcname).name
                data = archive.read(arcname)
                target.write_bytes(data)
                digest = _sha256_bytes(data)
                pdf_hashes[doc_id] = digest

            meta_lines = archive.read("meta_dump.jsonl").decode("utf-8").splitlines()
            records = []
            for line in meta_lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except ValueError:
                    continue

            for record in records:
                if record.get("type") == "document":
                    data = dict(record.get("data") or {})
                    doc_id = str(data.get("doc_id") or "").upper()
                    if not doc_id:
                        continue
                    if doc_id in pdf_hashes:
                        data["pdf_sha256"] = pdf_hashes[doc_id]
                        target = self.workspace / "storage" / doc_id
                        files = list(target.glob("*.pdf"))
                        if files:
                            try:
                                data["local_pdf_path"] = str(
                                    files[0].resolve().relative_to(self.workspace.resolve())
                                )
                            except ValueError:
                                data["local_pdf_path"] = str(files[0])
                        data["download_status"] = "DOWNLOADED"
                    else:
                        data.pop("local_pdf_path", None)
                        data.pop("pdf_sha256", None)
                    self.db.upsert_document(data)
                    counts["documents"] += 1
                elif record.get("type") == "knowledge_card":
                    data = dict(record.get("data") or {})
                    if not data.get("knw_id"):
                        continue
                    data["is_vectorized"] = 0
                    self.db.upsert_knowledge_card(data)
                    counts["cards"] += 1
                elif record.get("type") == "evidence":
                    data = dict(record.get("data") or {})
                    try:
                        self.db.add_evidence(
                            data["parent_knw_id"],
                            data["doc_id"],
                            data["context_bundle_md"],
                            section_title=data.get("section_title", ""),
                            page_num=data.get("page_num"),
                            chunk_id=data.get("chunk_id"),
                        )
                        counts["evidence"] += 1
                    except Exception:
                        log.exception("Evidence import skipped")
                elif record.get("type") == "keyword":
                    data = dict(record.get("data") or {})
                    self.db.upsert_keyword(
                        data.get("term_en", ""),
                        term_zh=data.get("term_zh", ""),
                        synonyms=data.get("synonyms_list") or data.get("synonyms_json"),
                        category=data.get("category", ""),
                        kw_id=data.get("kw_id"),
                    )
                    counts["keywords"] += 1
                elif record.get("type") == "card_keyword":
                    data = dict(record.get("data") or {})
                    try:
                        self.db.link_card_keywords(
                            data["knw_id"], [data["kw_id"]], weight=float(data.get("weight", 1.0))
                        )
                        counts["card_keywords"] += 1
                    except Exception:
                        log.exception("Keyword link import skipped")
                elif record.get("type") == "notation":
                    data = dict(record.get("data") or {})
                    self.db.upsert_notation(
                        data.get("raw_symbol", ""),
                        data.get("canonical_symbol", ""),
                        data.get("category", ""),
                    )
                    counts["notations"] += 1

            if self.qdrant is not None and getattr(self.qdrant, "available", False):
                signature = str(manifest.get("embedding_signature") or "")
                if signature and signature != self.db.embedding_signature():
                    log.warning(
                        "Vector import skipped: pack signature %s != local %s",
                        signature,
                        self.db.embedding_signature(),
                    )
                else:
                    records: list[dict[str, Any]] = []
                    if "vectors.parquet" in names:
                        try:
                            import io

                            import pyarrow.parquet as pq

                            table = pq.read_table(
                                io.BytesIO(archive.read("vectors.parquet"))
                            )
                            records = table.to_pylist()
                        except Exception:
                            log.exception("Parquet vector import skipped")
                    if not records and "vectors.jsonl" in names:
                        for line in archive.read("vectors.jsonl").decode("utf-8").splitlines():
                            if not line.strip():
                                continue
                            try:
                                records.append(json.loads(line))
                            except ValueError:
                                continue
                    items = []
                    for record in records:
                        if not record.get("knw_id") or not record.get("vector"):
                            continue
                        payload = record.get("payload") or {}
                        if isinstance(payload, str):
                            try:
                                payload = json.loads(payload)
                            except ValueError:
                                payload = {}
                        items.append(
                            (record["knw_id"], record["vector"], payload)
                        )
                    if items:
                        counts["vectors"] = self.qdrant.upsert_many(items)
                        self.db.mark_vectorized([item[0] for item in items], True)

        self.db.log_event("INFO", "acdb", f"导入知识包 {package.name}: {counts}")
        log.info("Imported %s: %s", package, counts)
        return counts
