"""SQLite3 + FTS5 persistence for AtomicCalculationDB V2.0.

Responsibilities
----------------
* documents ledger (download / parse state machine)
* L1 master cards and L2 knowledge cards
* raw evidence chunks (L3)
* controlled vocabulary, keyword links and notation normalization
* FTS5 BM25 index (external content table, kept in sync by triggers)
* task ledger, events, sync state and system metadata

Only the Python standard library is used here so the storage layer can be
tested without Qt or any AI dependency.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping

from .card_hash import compute_card_content_hash
from .id_generator import (
    CARD_TYPES,
    DOC_TYPES,
    IdError,
    classify_id,
    generate_doc_id,
    generate_evidence_id,
    generate_keyword_id,
    generate_knw_id,
    generate_master_id,
    normalize_card_type,
    normalize_doi,
    normalize_doc_type,
)

log = logging.getLogger(__name__)

SCHEMA_VERSION = "3.2"
DEFAULT_EMBEDDING_SIGNATURE = "BAAI/bge-m3::dim=1024"

DOWNLOAD_STATUSES = (
    "DISCOVERED",
    "QUEUED",
    "DOWNLOADING",
    "DOWNLOADED",
    "MANUAL_REQUIRED",
    "REJECTED",
    "FAILED",
)
PARSE_STATUSES = ("PENDING", "LIGHT_PARSED", "LLM_EXTRACTED", "FAILED")

_CJK_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿]+")
_FTS_TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)*")
_LIKE_ESCAPE_RE = re.compile(r"([%_\\])")


class EmbeddingSignatureMismatch(RuntimeError):
    """Raised when the configured embedding space differs from the stored one."""


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _load_json(value: Any, default: Any = None) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return default


def _normalize_list(value: Any, *, separators: str = r"[,;\n]+") -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [_clean(item) for item in value if _clean(item)]
    text = str(value).strip()
    if not text:
        return []
    parsed = _load_json(text)
    if isinstance(parsed, list):
        return [_clean(item) for item in parsed if _clean(item)]
    return [_clean(part) for part in re.split(separators, text) if _clean(part)]


def _normalize_metadata(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    parsed = _load_json(value)
    if isinstance(parsed, Mapping):
        return dict(parsed)
    return {"note": str(value)}


def _normalize_year(value: Any) -> int | None:
    if value in (None, ""):
        return None
    match = re.search(r"(\d{4})", str(value))
    if not match:
        return None
    year = int(match.group(1))
    return year if 1000 <= year <= 2999 else None


def _like_pattern(term: str) -> str:
    escaped = _LIKE_ESCAPE_RE.sub(r"\\\1", term)
    return f"%{escaped}%"


def _fts_query(text: str) -> str:
    tokens: list[str] = []
    seen: set[str] = set()
    for match in _FTS_TOKEN_RE.finditer(text):
        token = match.group(0).strip("-").lower()
        if len(token) < 2 or token in seen:
            continue
        seen.add(token)
        tokens.append(token.replace('"', '""'))
        if len(tokens) >= 16:
            break
    return " OR ".join(f'"{token}"' for token in tokens)


def _manual_snippet(text: str, term: str, width: int = 110) -> str:
    text = _clean(text)
    if not text:
        return ""
    index = text.lower().find(term.lower())
    if index < 0:
        return text[:width] + ("…" if len(text) > width else "")
    start = max(0, index - 45)
    end = min(len(text), index + len(term) + 65)
    return (
        ("…" if start > 0 else "")
        + text[start:index]
        + "\x01"
        + text[index : index + len(term)]
        + "\x02"
        + text[index + len(term) : end]
        + ("…" if end < len(text) else "")
    )


def build_search_payload(card: Mapping[str, Any]) -> str:
    """Assemble the bilingual retrieval payload from card fields."""
    ion_stage = _clean(card.get("ion_stage")) or "—"
    isoelectronic = _clean(card.get("isoelectronic")) or "—"
    z_num = card.get("z_num")
    method = _clean(card.get("method")) or "—"
    code_name = _clean(card.get("code_name")) or "—"
    process = _clean(card.get("physical_process")) or "—"
    keywords = _normalize_list(card.get("keywords") or card.get("tags") or [])
    return "\n".join(
        (
            f"[类型:{card.get('card_type', '—')} | 体系:{ion_stage} "
            f"({isoelectronic}, Z={z_num if z_num is not None else '—'}) | "
            f"方法:{method} ({code_name}) | 过程:{process}]",
            f"[前提与边界]: {_clean(card.get('premises_and_conditions'))}",
            f"[核心策略与结论(中)]: {_clean(card.get('summary_zh'))}",
            f"[标题]: {_clean(card.get('title_zh'))}",
            f"[Keywords]: {', '.join(keywords)}",
        )
    )


class SQLiteManager:
    """Thread-safe owner of ``workspace/atomic_meta.sqlite``."""

    def __init__(
        self,
        workspace_dir: str | Path,
        *,
        embedding_signature: str | None = None,
        schema_path: str | Path | None = None,
    ):
        self.workspace = Path(workspace_dir)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.db_path = self.workspace / "atomic_meta.sqlite"
        self.schema_path = (
            Path(schema_path)
            if schema_path
            else Path(__file__).with_name("schema_v2.sql")
        )
        self._lock = threading.RLock()
        self._tx_depth = 0
        self.embedding_signature_mismatch = False
        self.embedding_signature_stored = DEFAULT_EMBEDDING_SIGNATURE

        self._migrate_legacy_if_needed()
        self._is_new_database = not self.db_path.exists()
        self.conn = sqlite3.connect(
            str(self.db_path), check_same_thread=False, timeout=30.0
        )
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            self.conn.execute("PRAGMA foreign_keys = ON")
            self.conn.execute("PRAGMA journal_mode = WAL")
            self.conn.execute("PRAGMA synchronous = NORMAL")
            self._init_schema(embedding_signature)

    # ------------------------------------------------------------- migration
    def _migrate_legacy_if_needed(self) -> None:
        """Keep a V1 database as a timestamped backup before the V2 schema.

        The V2 schema reuses the ``knowledge_fts`` table name with different
        columns, so silently creating tables on top of V1 would corrupt the
        index.  Backup (never delete) is the safe path.
        """
        path = self.db_path
        if not path.exists():
            return
        backup_reason = ""
        try:
            conn = sqlite3.connect(path)
            try:
                rows = conn.execute(
                    "SELECT name, sql FROM sqlite_master WHERE type='table'"
                ).fetchall()
                tables = {row[0] for row in rows}
                if "meta" in tables and "system_meta" not in tables:
                    backup_reason = "V1 schema"
                elif "system_meta" in tables:
                    fts_sql = next(
                        (row[1] or "" for row in rows if row[0] == "knowledge_fts"),
                        "",
                    )
                    if fts_sql and "search_payload" not in fts_sql:
                        backup_reason = "legacy FTS layout"
            finally:
                conn.close()
        except sqlite3.DatabaseError:
            backup_reason = "unreadable database"
        if not backup_reason:
            return

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = path.with_name(f"atomic_meta_v1_backup_{stamp}.sqlite")
        try:
            os.replace(path, backup)
            for suffix in ("-wal", "-shm"):
                sidecar = Path(str(path) + suffix)
                if sidecar.exists():
                    sidecar.unlink()
            log.warning("Legacy database backed up to %s (%s)", backup, backup_reason)
        except OSError:
            log.exception("Could not back up legacy database; continuing")

    def _ensure_columns(self, table: str, columns: Mapping[str, str]) -> None:
        """Add missing columns to an existing database without losing data."""
        existing = {
            str(row["name"])
            for row in self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for name, ddl in columns.items():
            if name not in existing:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    def _backfill_content_hashes(self) -> None:
        """Fill v3 content fingerprints for cards created before the audit era."""
        if self.get_meta("content_hash_backfill_v3") == "1":
            return
        rows = self.conn.execute(
            "SELECT * FROM knowledge_cards WHERE content_hash = '' OR content_hash IS NULL"
        ).fetchall()
        for row in rows:
            card = self._card_dict(row)
            self.conn.execute(
                "UPDATE knowledge_cards SET content_hash = ? WHERE knw_id = ?",
                (compute_card_content_hash(card), card["knw_id"]),
            )
        self.set_meta("content_hash_backfill_v3", "1")

    def _backfill_document_pipeline_cache(self) -> None:
        """Backfill card_count, has_master, and pipeline status columns for documents."""
        if self.get_meta("doc_pipeline_cache_v280") == "1":
            return
        try:
            cards_summary = self.cards_summary_for_all_documents()
            rows = self.conn.execute(
                "SELECT doc_id, download_status, parse_status, local_pdf_path, local_md_path "
                "FROM documents"
            ).fetchall()
            from models.pipeline_stepper import compute_document_pipeline_status
            updates: list[tuple[int, int, int, str, str]] = []
            for row in rows:
                doc_id = str(row["doc_id"] or "").upper()
                summary = cards_summary.get(doc_id, {"card_count": 0, "has_master": False})
                st = compute_document_pipeline_status(
                    dict(row),
                    card_count=summary["card_count"],
                    has_master=summary["has_master"],
                )
                updates.append((
                    summary["card_count"],
                    1 if summary["has_master"] else 0,
                    st.current_step,
                    st.glow.value,
                    doc_id,
                ))
            if updates:
                self.conn.executemany(
                    "UPDATE documents SET card_count = ?, has_master = ?, "
                    "pipeline_step = ?, pipeline_glow = ? WHERE doc_id = ?",
                    updates,
                )
                self.conn.commit()
            self.set_meta("doc_pipeline_cache_v280", "1")
        except Exception:
            log.exception("Failed to backfill document pipeline cache")

    def _refresh_document_pipeline_cache(self, doc_ids: Iterable[str]) -> None:
        """Recalculate and update the cached pipeline state for given document IDs."""
        clean_ids = [str(d).strip().upper() for d in doc_ids if str(d).strip()]
        if not clean_ids:
            return
        from models.pipeline_stepper import compute_document_pipeline_status
        for doc_id in set(clean_ids):
            doc_row = self.conn.execute(
                "SELECT doc_id, download_status, parse_status, local_pdf_path, local_md_path "
                "FROM documents WHERE doc_id = ?",
                (doc_id,),
            ).fetchone()
            if not doc_row:
                continue
            card_rows = self.conn.execute(
                "SELECT card_level, card_type, supporting_doc_ids FROM knowledge_cards "
                "WHERE UPPER(supporting_doc_ids) LIKE ? ESCAPE '\\'",
                (f'%"{doc_id}"%',),
            ).fetchall()
            count = 0
            has_master = False
            for r in card_rows:
                try:
                    s_ids = _load_json(r["supporting_doc_ids"], [])
                    if doc_id in [str(x).upper() for x in s_ids]:
                        count += 1
                        if (
                            str(r["card_type"] or "").upper() == "MASTER"
                            or str(r["card_level"] or "").upper() == "L1"
                        ):
                            has_master = True
                except Exception:
                    pass
            st = compute_document_pipeline_status(
                dict(doc_row),
                card_count=count,
                has_master=has_master,
            )
            self.conn.execute(
                "UPDATE documents SET card_count = ?, has_master = ?, "
                "pipeline_step = ?, pipeline_glow = ? WHERE doc_id = ?",
                (count, 1 if has_master else 0, st.current_step, st.glow.value, doc_id),
            )

    # ------------------------------------------------------------- lifecycle
    def _init_schema(self, embedding_signature: str | None) -> None:
        schema = self.schema_path.read_text(encoding="utf-8")
        self.conn.executescript(schema)
        self._ensure_columns(
            "documents",
            {
                "input_tokens": "INTEGER NOT NULL DEFAULT 0",
                "output_tokens": "INTEGER NOT NULL DEFAULT 0",
                "parse_error_code": "TEXT NOT NULL DEFAULT ''",
                "card_count": "INTEGER NOT NULL DEFAULT 0",
                "has_master": "INTEGER NOT NULL DEFAULT 0",
                "pipeline_step": "INTEGER NOT NULL DEFAULT 1",
                "pipeline_glow": "TEXT NOT NULL DEFAULT 'NONE'",
            },
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_docs_pipeline "
            "ON documents(pipeline_step, pipeline_glow)"
        )
        self._ensure_columns(
            "knowledge_cards",
            {
                "is_user_verified": "INTEGER NOT NULL DEFAULT 0",
                "content_hash": "TEXT NOT NULL DEFAULT ''",
                "source_kind": "TEXT NOT NULL DEFAULT 'EXTRACTION'",
                "audit_status": "TEXT NOT NULL DEFAULT 'NORMAL'",
                "audit_delete_suggested": "INTEGER NOT NULL DEFAULT 0",
                "audit_hidden": "INTEGER NOT NULL DEFAULT 0",
                "audit_delete_reason": "TEXT NOT NULL DEFAULT ''",
                "audit_delete_by": "TEXT NOT NULL DEFAULT ''",
                "audit_delete_at": "TEXT",
                "audit_review_count": "INTEGER NOT NULL DEFAULT 0",
                "audit_positive_count": "INTEGER NOT NULL DEFAULT 0",
                "audit_negative_count": "INTEGER NOT NULL DEFAULT 0",
                "audit_score_min": "INTEGER",
                "audit_score_max": "INTEGER",
                "audit_divergence": "INTEGER NOT NULL DEFAULT 0",
                "needs_vector_update": "INTEGER NOT NULL DEFAULT 0",
                "master_stale": "INTEGER NOT NULL DEFAULT 0",
            },
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_knw_audit "
            "ON knowledge_cards(audit_hidden, audit_status, audit_divergence)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_knw_vector_pending "
            "ON knowledge_cards(needs_vector_update)"
        )
        self._ensure_columns(
            "raw_evidence_chunks",
            {
                "bundle_hash": "TEXT NOT NULL DEFAULT ''",
                "locator_json": "TEXT NOT NULL DEFAULT '{}'",
            },
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_evidence_hash "
            "ON raw_evidence_chunks(doc_id, bundle_hash)"
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS collections (
                collection_id TEXT PRIMARY KEY,
                parent_id     TEXT REFERENCES collections(collection_id) ON DELETE CASCADE,
                name          TEXT NOT NULL,
                icon          TEXT NOT NULL DEFAULT 'folder',
                sort_order    INTEGER NOT NULL DEFAULT 0,
                created_at    TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
            )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS collection_documents (
                collection_id TEXT NOT NULL REFERENCES collections(collection_id) ON DELETE CASCADE,
                doc_id        TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
                added_at      TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (collection_id, doc_id)
            )"""
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_coll_docs_doc ON collection_documents(doc_id)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_coll_parent ON collections(parent_id)")
        self._backfill_content_hashes()
        self._backfill_document_pipeline_cache()
        for key, column in (
            ("total_input_tokens", "input_tokens"),
            ("total_output_tokens", "output_tokens"),
        ):
            existing_row = self.conn.execute(
                "SELECT 1 FROM system_meta WHERE key = ?", (key,)
            ).fetchone()
            if existing_row is None:
                total = self.conn.execute(
                    f"SELECT COALESCE(SUM({column}), 0) FROM documents"
                ).fetchone()[0]
                self.conn.execute(
                    "INSERT INTO system_meta(key, value, updated_at) VALUES (?, ?, ?)",
                    (key, str(int(total or 0)), now_iso()),
                )
        self.conn.execute(
            "INSERT OR REPLACE INTO system_meta(key, value, updated_at) "
            "VALUES ('schema_version', ?, ?)",
            (SCHEMA_VERSION, now_iso()),
        )

        stored = self.get_meta("embedding_signature") or DEFAULT_EMBEDDING_SIGNATURE
        if embedding_signature:
            if self._is_new_database:
                stored = embedding_signature
                self.set_meta("embedding_signature", stored)
            elif stored != embedding_signature:
                self.embedding_signature_mismatch = True
                log.error(
                    "Embedding signature mismatch: stored=%s configured=%s",
                    stored,
                    embedding_signature,
                )
        self.embedding_signature_stored = stored

        if self.get_meta("fts_rebuilt_v2") != "1":
            self.conn.execute(
                "INSERT INTO knowledge_fts(knowledge_fts) VALUES('rebuild')"
            )
            self.set_meta("fts_rebuilt_v2", "1")
        self.conn.commit()

    def _commit(self) -> None:
        if self._tx_depth <= 0:
            self.conn.commit()

    @contextlib.contextmanager
    def transaction(self):
        """Execute multiple DB operations atomically inside one SQLite transaction."""
        with self._lock:
            outer = self._tx_depth == 0
            if outer and not self.conn.in_transaction:
                self.conn.execute("BEGIN")
            self._tx_depth += 1
            try:
                yield
            except Exception:
                self._tx_depth -= 1
                if outer and self.conn.in_transaction:
                    self.conn.rollback()
                raise
            else:
                self._tx_depth -= 1
                if outer and self.conn.in_transaction:
                    self.conn.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self.conn.commit()
            finally:
                self.conn.close()

    def vacuum(self) -> None:
        with self._lock:
            self.conn.commit()
            self.conn.execute("VACUUM")

    def backup_to(self, target: str | Path) -> Path:
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            destination = sqlite3.connect(str(target))
            try:
                self.conn.backup(destination)
            finally:
                destination.close()
        return target

    # ------------------------------------------------------------------ meta
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT value FROM system_meta WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO system_meta(key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at",
                (key, value, now_iso()),
            )
            self._commit()

    def embedding_signature(self) -> str:
        return self.embedding_signature_stored

    def assert_embedding_signature(self, expected: str) -> None:
        if self.embedding_signature_stored != expected:
            raise EmbeddingSignatureMismatch(
                f"Embedding 空间已锁定为 {self.embedding_signature_stored}，"
                f"当前配置为 {expected}。请重建向量库后切换。"
            )

    # ------------------------------------------------------------- documents
    def upsert_document(
        self, document: Mapping[str, Any], *, generate_missing: bool = True
    ) -> str:
        """Insert or partially merge a document, returning its DOC-ID."""
        with self._lock:
            doc = dict(document)
            provided = set(doc.keys())
            title_in = _clean(doc.get("title")) if "title" in provided else ""
            type_in = (
                normalize_doc_type(doc.get("doc_type"))
                if "doc_type" in provided
                else None
            )
            year_provided = "pub_year" in provided or "year" in provided
            year_in = (
                _normalize_year(doc.get("pub_year", doc.get("year")))
                if year_provided
                else None
            )
            doi_in = (
                (normalize_doi(doc.get("doi")) or None) if "doi" in provided else None
            )
            hash_in = (
                _clean(doc.get("pdf_sha256")) if "pdf_sha256" in provided else ""
            )

            doc_id = _clean(doc.get("doc_id")).upper() or None
            existing = self._row_document(doc_id) if doc_id else None

            if doi_in:
                row = self.conn.execute(
                    "SELECT * FROM documents WHERE doi = ?", (doi_in,)
                ).fetchone()
                if row is not None:
                    if doc_id and doc_id != row["doc_id"]:
                        log.warning(
                            "DOI %s already maps to %s; merging into existing record",
                            doi_in,
                            row["doc_id"],
                        )
                    doc_id = row["doc_id"]
                    existing = dict(row)
            if not doc_id and hash_in:
                row = self.conn.execute(
                    "SELECT * FROM documents WHERE pdf_sha256 = ? LIMIT 1",
                    (hash_in,),
                ).fetchone()
                if row is not None:
                    doc_id = row["doc_id"]
                    existing = dict(row)
            if not doc_id:
                if not generate_missing:
                    raise IdError("doc_id 缺失且未启用自动生成")
                if not title_in:
                    raise ValueError("新增文献必须提供标题")
                doc_id = generate_doc_id(
                    type_in or "PAP", year_in, doi=doi_in, title=title_in
                )
                row = self._row_document(doc_id)
                if row is not None:
                    existing = row
            if classify_id(doc_id) is None or classify_id(doc_id)["family"] != "document":
                raise IdError(f"非法 DOC-ID: {doc_id}")

            final_title = title_in or (existing["title"] if existing else "")
            if not final_title:
                raise ValueError("文献标题不能为空")

            if type_in:
                final_type = type_in
            elif existing:
                final_type = existing["doc_type"]
            else:
                final_type = "PAP"
            if final_type not in DOC_TYPES:
                final_type = "PAP"

            final_year = (
                year_in
                if year_provided
                else (existing["pub_year"] if existing else None)
            )
            final_doi = (
                doi_in if "doi" in provided else (existing["doi"] if existing else None)
            )

            def pick(key: str, default: Any = None) -> Any:
                if key in provided:
                    return doc.get(key)
                if existing is not None:
                    return existing.get(key)
                return default

            authors = (
                _json_dump(_normalize_list(doc.get("authors"), separators=r"[;\n]+"))
                if "authors" in provided
                else (existing["authors"] if existing else "[]")
            )
            keywords_json = (
                _json_dump(_normalize_list(doc.get("keywords")))
                if ("keywords" in provided or "keywords_json" in provided)
                else (existing["keywords_json"] if existing else "[]")
            )
            metadata = (
                _normalize_metadata(doc.get("metadata_json"))
                if "metadata_json" in provided
                else _normalize_metadata(existing["metadata_json"] if existing else None)
            )
            if existing and "metadata_json" in provided:
                previous = _load_json(existing.get("metadata_json"), {})
                if isinstance(previous, dict):
                    previous.update(metadata)
                    metadata = previous

            download_status = str(pick("download_status", "QUEUED") or "QUEUED").upper()
            if download_status not in DOWNLOAD_STATUSES:
                download_status = "QUEUED"
            parse_status = str(pick("parse_status", "PENDING") or "PENDING").upper()
            if parse_status not in PARSE_STATUSES:
                parse_status = "PENDING"

            authority_default = (
                1.5 if final_type in ("BOK", "REV") else 1.2 if final_type == "MAN" else 1.0
            )
            authority = pick("authority_weight", authority_default)
            try:
                authority = float(authority)
            except (TypeError, ValueError):
                authority = authority_default
            try:
                gatekeeper = int(pick("gatekeeper_score", 0) or 0)
            except (TypeError, ValueError):
                gatekeeper = 0
            gatekeeper = max(0, min(100, gatekeeper))

            try:
                input_tokens = int(pick("input_tokens", 0) or 0)
            except (TypeError, ValueError):
                input_tokens = 0
            try:
                output_tokens = int(pick("output_tokens", 0) or 0)
            except (TypeError, ValueError):
                output_tokens = 0
            input_tokens = max(0, input_tokens)
            output_tokens = max(0, output_tokens)

            values = {
                "doc_id": doc_id,
                "doi": final_doi,
                "arxiv_id": _clean(pick("arxiv_id", "")) or None,
                "title": final_title,
                "authors": authors,
                "journal": _clean(pick("journal", "")),
                "pub_year": final_year,
                "doc_type": final_type,
                "gatekeeper_score": gatekeeper,
                "authority_weight": authority,
                "download_status": download_status,
                "parse_status": parse_status,
                "source_url": _clean(pick("source_url", "")) or None,
                "local_pdf_path": _clean(pick("local_pdf_path", "")) or None,
                "local_md_path": _clean(pick("local_md_path", "")) or None,
                "pdf_sha256": _clean(pick("pdf_sha256", "")) or None,
                "abstract": str(pick("abstract", "") or "").strip(),
                "keywords_json": keywords_json,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "metadata_json": _json_dump(metadata),
                "error_log": pick("error_log"),
                "parse_error_code": _clean(pick("parse_error_code", "")).upper(),
                "created_at": (
                    existing["created_at"]
                    if existing
                    else (doc.get("created_at") or now_iso())
                ),
                "updated_at": now_iso(),
            }
            self.conn.execute(
                """
                INSERT INTO documents (
                    doc_id, doi, arxiv_id, title, authors, journal, pub_year,
                    doc_type, gatekeeper_score, authority_weight, download_status,
                    parse_status, source_url, local_pdf_path, local_md_path,
                    pdf_sha256, abstract, keywords_json, input_tokens, output_tokens,
                    metadata_json, error_log, parse_error_code, created_at, updated_at
                ) VALUES (
                    :doc_id, :doi, :arxiv_id, :title, :authors, :journal, :pub_year,
                    :doc_type, :gatekeeper_score, :authority_weight, :download_status,
                    :parse_status, :source_url, :local_pdf_path, :local_md_path,
                    :pdf_sha256, :abstract, :keywords_json, :input_tokens, :output_tokens,
                    :metadata_json, :error_log, :parse_error_code, :created_at, :updated_at
                )
                ON CONFLICT(doc_id) DO UPDATE SET
                    doi=excluded.doi,
                    arxiv_id=excluded.arxiv_id,
                    title=excluded.title,
                    authors=excluded.authors,
                    journal=excluded.journal,
                    pub_year=excluded.pub_year,
                    doc_type=excluded.doc_type,
                    gatekeeper_score=excluded.gatekeeper_score,
                    authority_weight=excluded.authority_weight,
                    download_status=excluded.download_status,
                    parse_status=excluded.parse_status,
                    source_url=excluded.source_url,
                    local_pdf_path=excluded.local_pdf_path,
                    local_md_path=excluded.local_md_path,
                    pdf_sha256=excluded.pdf_sha256,
                    abstract=excluded.abstract,
                    keywords_json=excluded.keywords_json,
                    input_tokens=excluded.input_tokens,
                    output_tokens=excluded.output_tokens,
                    metadata_json=excluded.metadata_json,
                    error_log=excluded.error_log,
                    parse_error_code=excluded.parse_error_code,
                    updated_at=excluded.updated_at
                """,
                values,
            )
            self._commit()
            self._refresh_document_pipeline_cache([doc_id])
            self._commit()
            if existing is None:
                self.log_event(
                    "INFO", "document", f"新增文献 {doc_id}: {final_title}", doc_id=doc_id
                )
            return doc_id

    def _row_document(self, doc_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM documents WHERE doc_id = ?", (doc_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_document(self, doc_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._row_document(_clean(doc_id).upper())
            return self._document_dict(row) if row else None

    def find_document_by_doi(self, doi: str) -> dict[str, Any] | None:
        normalized = normalize_doi(doi)
        if not normalized:
            return None
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM documents WHERE doi = ?", (normalized,)
            ).fetchone()
            return self._document_dict(row) if row else None

    def find_document_by_pdf_hash(self, sha256: str) -> dict[str, Any] | None:
        value = _clean(sha256)
        if not value:
            return None
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM documents WHERE pdf_sha256 = ? LIMIT 1", (value,)
            ).fetchone()
            return self._document_dict(row) if row else None

    def list_documents(
        self,
        *,
        download_status: str | None = None,
        parse_status: str | None = None,
        parse_statuses: Iterable[str] | None = None,
        doc_type: str | None = None,
        text: str | None = None,
        limit: int = 200,
        offset: int = 0,
        order_by: str = "updated_at DESC",
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if download_status:
            clauses.append("download_status = ?")
            params.append(download_status.upper())
        if parse_status:
            clauses.append("parse_status = ?")
            params.append(parse_status.upper())
        if parse_statuses:
            values = [str(v).upper() for v in parse_statuses if v]
            if values:
                clauses.append(f"parse_status IN ({','.join('?' for _ in values)})")
                params.extend(values)
        if doc_type:
            clauses.append("doc_type = ?")
            params.append(normalize_doc_type(doc_type))
        if text:
            pattern = _like_pattern(text)
            clauses.append(
                "(title LIKE ? ESCAPE '\\' OR abstract LIKE ? ESCAPE '\\' "
                "OR authors LIKE ? ESCAPE '\\')"
            )
            params.extend([pattern, pattern, pattern])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        allowed_orders = {
            "updated_at DESC",
            "updated_at ASC",
            "created_at DESC",
            "pub_year DESC",
        }
        order_by = order_by if order_by in allowed_orders else "updated_at DESC"
        params.extend([max(1, min(int(limit), 2000)), max(0, int(offset))])
        sql = (
            f"SELECT * FROM documents {where} ORDER BY {order_by}, doc_id "
            "LIMIT ? OFFSET ?"
        )
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
            return [self._document_dict(row) for row in rows]

    def list_documents_summary(
        self,
        *,
        collection_id: str | None = None,
        download_status: str | None = None,
        parse_status: str | None = None,
        parse_statuses: Iterable[str] | None = None,
        doc_type: str | None = None,
        text: str | None = None,
        limit: int = 50,
        offset: int = 0,
        order_by: str = "updated_at DESC",
    ) -> list[dict[str, Any]]:
        """Return lean summary dicts for table display without heavy text/JSON columns."""
        clauses: list[str] = []
        params: list[Any] = []
        if collection_id == "__unfiled__":
            clauses.append("doc_id NOT IN (SELECT doc_id FROM collection_documents)")
        elif collection_id == "__recent__":
            clauses.append("created_at >= datetime('now', '-7 days')")
        elif collection_id and collection_id not in ("__all__", ""):
            clauses.append("doc_id IN (SELECT doc_id FROM collection_documents WHERE collection_id = ?)")
            params.append(collection_id)

        if download_status:
            clauses.append("download_status = ?")
            params.append(download_status.upper())
        if parse_status:
            clauses.append("parse_status = ?")
            params.append(parse_status.upper())
        if parse_statuses:
            values = [str(v).upper() for v in parse_statuses if v]
            if values:
                clauses.append(f"parse_status IN ({','.join('?' for _ in values)})")
                params.extend(values)
        if doc_type:
            clauses.append("doc_type = ?")
            params.append(normalize_doc_type(doc_type))
        if text:
            pattern = _like_pattern(text)
            clauses.append(
                "(title LIKE ? ESCAPE '\\' OR doc_id LIKE ? ESCAPE '\\' "
                "OR journal LIKE ? ESCAPE '\\' OR authors LIKE ? ESCAPE '\\')"
            )
            params.extend([pattern, pattern, pattern, pattern])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        allowed_orders = {
            "updated_at DESC", "updated_at ASC",
            "created_at DESC", "created_at ASC",
            "pub_year DESC", "pub_year ASC",
            "doc_id DESC", "doc_id ASC",
            "title DESC", "title ASC",
            "authors DESC", "authors ASC",
            "doc_type DESC", "doc_type ASC",
            "journal DESC", "journal ASC",
            "input_tokens DESC", "input_tokens ASC",
            "download_status DESC", "download_status ASC",
            "parse_status DESC", "parse_status ASC",
            "pipeline_step DESC", "pipeline_step ASC",
            "pipeline_step DESC, pipeline_glow DESC",
            "pipeline_step ASC, pipeline_glow ASC",
            "card_count DESC", "card_count ASC",
        }
        order_by = order_by if order_by in allowed_orders else "updated_at DESC"
        params.extend([max(1, min(int(limit), 2000)), max(0, int(offset))])
        sql = (
            f"SELECT doc_id, doi, title, authors, doc_type, pub_year, journal, "
            f"input_tokens, output_tokens, download_status, parse_status, "
            f"parse_error_code, local_pdf_path, local_md_path, "
            f"card_count, has_master, pipeline_step, pipeline_glow, "
            f"created_at, updated_at "
            f"FROM documents {where} ORDER BY {order_by}, doc_id "
            f"LIMIT ? OFFSET ?"
        )
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
            return [dict(row) for row in rows]

    def count_documents(
        self,
        download_status: str | None = None,
        parse_status: str | None = None,
        text: str | None = None,
        collection_id: str | None = None,
    ) -> int:
        clauses: list[str] = []
        params: list[Any] = []
        if collection_id == "__unfiled__":
            clauses.append("doc_id NOT IN (SELECT doc_id FROM collection_documents)")
        elif collection_id == "__recent__":
            clauses.append("created_at >= datetime('now', '-7 days')")
        elif collection_id and collection_id not in ("__all__", ""):
            clauses.append("doc_id IN (SELECT doc_id FROM collection_documents WHERE collection_id = ?)")
            params.append(collection_id)

        if download_status:
            clauses.append("download_status = ?")
            params.append(download_status.upper())
        if parse_status:
            clauses.append("parse_status = ?")
            params.append(parse_status.upper())
        if text:
            pattern = _like_pattern(text)
            clauses.append(
                "(title LIKE ? ESCAPE '\\' OR doc_id LIKE ? ESCAPE '\\' "
                "OR journal LIKE ? ESCAPE '\\' OR authors LIKE ? ESCAPE '\\')"
            )
            params.extend([pattern, pattern, pattern, pattern])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            return int(
                self.conn.execute(
                    f"SELECT COUNT(*) FROM documents {where}", params
                ).fetchone()[0]
            )

    # ----------------------------------------------------------- collections (Zotero-style)
    def create_collection(
        self,
        name: str,
        parent_id: str | None = None,
        icon: str = "folder",
    ) -> str:
        clean_name = str(name).strip()
        if not clean_name:
            raise ValueError("分类目录名称不能为空")
        col_id = f"col_{uuid.uuid4().hex[:12]}"
        now = now_iso()
        with self._lock:
            if parent_id:
                max_order = self.conn.execute(
                    "SELECT COALESCE(MAX(sort_order), 0) FROM collections WHERE parent_id = ?",
                    (parent_id,),
                ).fetchone()[0]
            else:
                max_order = self.conn.execute(
                    "SELECT COALESCE(MAX(sort_order), 0) FROM collections WHERE parent_id IS NULL",
                ).fetchone()[0]
            self.conn.execute(
                "INSERT INTO collections (collection_id, parent_id, name, icon, sort_order, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (col_id, parent_id or None, clean_name, icon, int(max_order) + 1, now, now),
            )
            self._commit()
        return col_id

    def rename_collection(self, collection_id: str, new_name: str) -> bool:
        clean_name = str(new_name).strip()
        if not clean_name:
            raise ValueError("分类目录名称不能为空")
        with self._lock:
            cur = self.conn.execute(
                "UPDATE collections SET name = ?, updated_at = ? WHERE collection_id = ?",
                (clean_name, now_iso(), collection_id),
            )
            self._commit()
            return cur.rowcount > 0

    def delete_collection(self, collection_id: str) -> bool:
        with self._lock:
            children = [
                row[0] for row in self.conn.execute(
                    "SELECT collection_id FROM collections WHERE parent_id = ?", (collection_id,)
                ).fetchall()
            ]
            for child_id in children:
                self.delete_collection(child_id)
            self.conn.execute(
                "DELETE FROM collection_documents WHERE collection_id = ?", (collection_id,)
            )
            cur = self.conn.execute(
                "DELETE FROM collections WHERE collection_id = ?", (collection_id,)
            )
            self._commit()
            return cur.rowcount > 0

    def list_collections(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT c.collection_id, c.parent_id, c.name, c.icon, c.sort_order, "
                "c.created_at, c.updated_at, "
                "(SELECT COUNT(*) FROM collection_documents cd WHERE cd.collection_id = c.collection_id) AS doc_count "
                "FROM collections c ORDER BY c.sort_order ASC, c.name ASC"
            ).fetchall()
            return [dict(row) for row in rows]

    def add_documents_to_collection(
        self,
        collection_id: str,
        doc_ids: Iterable[str],
    ) -> int:
        clean_ids = [str(did).strip().upper() for did in doc_ids if str(did).strip()]
        if not clean_ids:
            return 0
        now = now_iso()
        added = 0
        with self._lock:
            for doc_id in clean_ids:
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO collection_documents (collection_id, doc_id, added_at) VALUES (?, ?, ?)",
                    (collection_id, doc_id, now),
                )
                if cur.rowcount > 0:
                    added += 1
            if added > 0:
                self._commit()
        return added

    def remove_documents_from_collection(
        self,
        collection_id: str,
        doc_ids: Iterable[str],
    ) -> int:
        clean_ids = [str(did).strip().upper() for did in doc_ids if str(did).strip()]
        if not clean_ids:
            return 0
        removed = 0
        with self._lock:
            for doc_id in clean_ids:
                cur = self.conn.execute(
                    "DELETE FROM collection_documents WHERE collection_id = ? AND doc_id = ?",
                    (collection_id, doc_id),
                )
                if cur.rowcount > 0:
                    removed += 1
            if removed > 0:
                self._commit()
        return removed

    def get_document_collection_ids(self, doc_id: str) -> list[str]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT collection_id FROM collection_documents WHERE doc_id = ? ORDER BY added_at ASC",
                (doc_id.upper(),),
            ).fetchall()
            return [row[0] for row in rows]

    def count_collection_documents(self, collection_id: str) -> int:
        with self._lock:
            if collection_id == "__unfiled__":
                return int(
                    self.conn.execute(
                        "SELECT COUNT(*) FROM documents WHERE doc_id NOT IN (SELECT doc_id FROM collection_documents)"
                    ).fetchone()[0]
                )
            if collection_id == "__recent__":
                return int(
                    self.conn.execute(
                        "SELECT COUNT(*) FROM documents WHERE created_at >= datetime('now', '-7 days')"
                    ).fetchone()[0]
                )
            if collection_id in ("__all__", "", None):
                return int(self.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0])
            return int(
                self.conn.execute(
                    "SELECT COUNT(*) FROM collection_documents WHERE collection_id = ?",
                    (collection_id,),
                ).fetchone()[0]
            )

    def update_download_status(
        self,
        doc_id: str,
        status: str,
        *,
        error: str | None = None,
        source_url: str | None = None,
    ) -> None:
        value = status.upper()
        if value not in DOWNLOAD_STATUSES:
            raise ValueError(f"未知下载状态: {status}")
        with self._lock:
            self.conn.execute(
                "UPDATE documents SET download_status=?, error_log=?, "
                "source_url=COALESCE(?, source_url), updated_at=? WHERE doc_id=?",
                (value, error, source_url, now_iso(), doc_id.upper()),
            )
            self._commit()
            self._refresh_document_pipeline_cache([doc_id])
            self._commit()
        self.log_event(
            "ERROR" if value == "FAILED" else "INFO",
            "document",
            f"{doc_id} download -> {value}" + (f" ({error})" if error else ""),
            doc_id=doc_id,
        )

    def update_parse_status(
        self,
        doc_id: str,
        status: str,
        error: str | None = None,
        *,
        error_code: str = "",
    ) -> None:
        value = status.upper()
        if value not in PARSE_STATUSES:
            raise ValueError(f"未知解析状态: {status}")
        with self._lock:
            self.conn.execute(
                "UPDATE documents SET parse_status=?, error_log=?, "
                "parse_error_code=?, updated_at=? WHERE doc_id=?",
                (value, error, _clean(error_code).upper(), now_iso(), doc_id.upper()),
            )
            self._commit()
            self._refresh_document_pipeline_cache([doc_id])
            self._commit()
        self.log_event(
            "ERROR" if value == "FAILED" else "INFO",
            "document",
            f"{doc_id} parse -> {value}"
            + (f" [{_clean(error_code).upper()}]" if error_code else "")
            + (f" ({error})" if error else ""),
            doc_id=doc_id,
        )

    def attach_pdf(
        self,
        doc_id: str,
        pdf_path: str | Path,
        *,
        sha256: str | None = None,
        source_url: str | None = None,
    ) -> str:
        """Attach a downloaded PDF and queue it for parsing."""
        path = Path(pdf_path)
        try:
            stored_path = str(path.resolve().relative_to(self.workspace.resolve()))
        except ValueError:
            stored_path = str(path.resolve())
        with self._lock:
            self.conn.execute(
                """
                UPDATE documents SET local_pdf_path=?, pdf_sha256=?,
                    download_status='DOWNLOADED', parse_status='PENDING',
                    parse_error_code='', error_log=NULL,
                    source_url=COALESCE(?, source_url), updated_at=?
                WHERE doc_id=?
                """,
                (stored_path, sha256, source_url, now_iso(), doc_id.upper()),
            )
            self._commit()
        self.add_task("PARSE", doc_id=doc_id, priority=10)
        self.log_event("INFO", "document", f"{doc_id} PDF attached: {stored_path}", doc_id=doc_id)
        return stored_path

    def set_local_md_path(self, doc_id: str, md_path: str | Path) -> None:
        path = Path(md_path)
        try:
            stored_path = str(path.resolve().relative_to(self.workspace.resolve()))
        except ValueError:
            stored_path = str(path.resolve())
        with self._lock:
            self.conn.execute(
                "UPDATE documents SET local_md_path=?, updated_at=? WHERE doc_id=?",
                (stored_path, now_iso(), doc_id.upper()),
            )
            self._commit()

    def clear_demo_documents(self) -> int:
        """Delete demo documents and every knowledge card derived only from them.

        Foreign keys cascade evidence and document-bound tasks.  Cards keep
        their source IDs in JSON, so they are handled explicitly: a card that
        cites only demo documents is removed, while a card that also cites a
        real document stays and simply loses the demo reference.
        """
        with self._lock:
            rows = self.conn.execute(
                "SELECT doc_id FROM documents WHERE metadata_json LIKE '%\"demo\":true%'"
            ).fetchall()
            doc_ids = {row["doc_id"] for row in rows}
            # Demo-only queue entries are removed even if their documents were
            # already deleted individually.
            self.conn.execute(
                "DELETE FROM tasks WHERE payload_json LIKE '%\"demo\":true%'"
            )
            if not doc_ids:
                self.conn.execute("DELETE FROM events WHERE category='demo'")
                self._purge_demo_keywords_locked()
                self._commit()
                return 0

            card_rows = self.conn.execute(
                "SELECT knw_id, supporting_doc_ids FROM knowledge_cards"
            ).fetchall()
            for row in card_rows:
                supporting = {
                    str(item)
                    for item in _load_json(row["supporting_doc_ids"], []) or []
                }
                if not supporting & doc_ids:
                    continue
                remaining = sorted(supporting - doc_ids)
                if remaining:
                    self.conn.execute(
                        "UPDATE knowledge_cards SET supporting_doc_ids=?, updated_at=? "
                        "WHERE knw_id=?",
                        (_json_dump(remaining), now_iso(), row["knw_id"]),
                    )
                else:
                    self.conn.execute(
                        "DELETE FROM knowledge_cards WHERE knw_id=?", (row["knw_id"],)
                    )

            placeholders = ",".join("?" for _ in doc_ids)
            params = tuple(sorted(doc_ids))
            self.conn.execute(
                f"DELETE FROM documents WHERE doc_id IN ({placeholders})", params
            )
            self.conn.execute(
                f"DELETE FROM events WHERE category='demo' OR doc_id IN ({placeholders})",
                params,
            )
            self._purge_demo_keywords_locked()
            self._commit()
        return len(doc_ids)

    def _purge_demo_keywords_locked(self) -> None:
        """Drop demo-seeded keywords that no remaining card links to."""
        row = self.conn.execute(
            "SELECT value FROM system_meta WHERE key='demo_keyword_ids'"
        ).fetchone()
        if not row:
            return
        for kw_id in (item.strip() for item in str(row["value"]).split(",")):
            if not kw_id:
                continue
            self.conn.execute(
                "DELETE FROM controlled_keywords WHERE kw_id=? AND NOT EXISTS "
                "(SELECT 1 FROM card_keywords WHERE kw_id=?)",
                (kw_id, kw_id),
            )
        self.conn.execute(
            "DELETE FROM system_meta WHERE key='demo_keyword_ids'"
        )

    def delete_document(self, doc_id: str) -> bool:
        """Delete one document; foreign keys cascade its evidence and tasks."""
        doc_id = _clean(doc_id).upper()
        with self._lock:
            if not self._row_document(doc_id):
                return False
            self.conn.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,))
            self._commit()
        self.log_event("INFO", "document", f"删除文献 {doc_id}", doc_id=doc_id)
        return True

    # ---------------------------------------------------------- knowledge cards
    def upsert_knowledge_card(
        self, card: Mapping[str, Any], *, generate_missing: bool = True
    ) -> str:
        """Insert or partially merge an L1/L2 knowledge card."""
        with self._lock:
            data = dict(card)
            provided = set(data.keys())
            knw_id = _clean(data.get("knw_id")).upper() or None
            existing = None
            if knw_id:
                row = self.conn.execute(
                    "SELECT * FROM knowledge_cards WHERE knw_id = ?", (knw_id,)
                ).fetchone()
                existing = dict(row) if row else None

            level = str(
                (data.get("card_level") if "card_level" in provided else None)
                or (existing["card_level"] if existing else "L2")
            ).upper()
            if level not in ("L1", "L2"):
                level = "L2"
            if "card_type" in provided:
                card_type = normalize_card_type(data.get("card_type"), level=level)
            elif existing:
                card_type = existing["card_type"]
            else:
                card_type = "MASTER" if level == "L1" else "CONC"
            if level == "L1":
                card_type = "MASTER"
            if card_type not in CARD_TYPES + ("MASTER",):
                card_type = "CONC"

            title_en = _clean(data.get("title_en")) if "title_en" in provided else (
                existing["title_en"] if existing else ""
            )
            title_zh = _clean(data.get("title_zh")) if "title_zh" in provided else ""
            if not title_zh and existing:
                title_zh = existing["title_zh"]
            if not title_zh:
                title_zh = title_en
            if not title_zh:
                raise ValueError("知识卡片必须提供 title_zh 或 title_en")

            def pick(key: str, default: Any = None) -> Any:
                if key in provided:
                    return data.get(key)
                if existing is not None:
                    return existing.get(key)
                return default

            doc_ids = _normalize_list(pick("supporting_doc_ids", []))
            conflicts = _normalize_list(pick("conflict_with_knw_ids", []))
            summary = str(pick("summary_zh", "") or "").strip()
            premises = str(pick("premises_and_conditions", "") or "").strip()
            formulas = str(pick("formulas_latex", "") or "")
            code_snippet = str(pick("code_snippet", "") or "")
            pitfall = str(pick("pitfall_warning", "") or "")
            structured = _normalize_metadata(pick("structured_details_json", {}))
            if "generalized_conjecture" in provided:
                val = data.get("generalized_conjecture")
                if val:
                    structured["generalized_conjecture"] = str(val).strip()
                elif val is None and "generalized_conjecture" in structured:
                    structured.pop("generalized_conjecture", None)
            elif existing and "generalized_conjecture" not in structured:
                ext_struc = _load_json(existing.get("structured_details_json"), {})
                if "generalized_conjecture" in ext_struc:
                    structured["generalized_conjecture"] = ext_struc["generalized_conjecture"]
            current_year = datetime.now().year
            default_year = _normalize_year(pick("card_year")) or _normalize_year(
                pick("recency_year")
            ) or self._max_pub_year(doc_ids) or current_year

            if not knw_id:
                if not generate_missing:
                    raise IdError("knw_id 缺失且未启用自动生成")
                if level == "L1":
                    topic = (
                        _clean(pick("isoelectronic", ""))
                        or _clean(pick("element", ""))
                        or _clean(pick("ion_stage", ""))
                        or "GENERAL"
                    )
                    method = _clean(pick("method", "")) or "GENERAL"
                    knw_id = generate_master_id(topic, method)
                else:
                    knw_id = generate_knw_id(
                        card_type,
                        default_year,
                        title=title_zh,
                        summary=summary,
                        supporting_doc_ids=doc_ids,
                        level=level,
                    )
                row = self.conn.execute(
                    "SELECT * FROM knowledge_cards WHERE knw_id = ?", (knw_id,)
                ).fetchone()
                if row is not None:
                    existing = dict(row)
            if classify_id(knw_id) is None:
                raise IdError(f"非法 KNW-ID: {knw_id}")

            content_fields = {
                "title_zh",
                "title_en",
                "summary_zh",
                "premises_and_conditions",
                "formulas_latex",
                "code_snippet",
                "pitfall_warning",
                "structured_details_json",
                "element",
                "z_num",
                "ion_stage",
                "isoelectronic",
                "shell_type",
                "method",
                "code_name",
                "physical_process",
                "keywords",
                "tags",
                "card_type",
            }
            if "search_payload" in provided:
                search_payload = str(data.get("search_payload") or "").strip()
            elif existing and not (provided & content_fields):
                search_payload = str(existing.get("search_payload") or "").strip()
            else:
                search_payload = ""
            if not search_payload:
                search_payload = build_search_payload(
                    {
                        "card_type": card_type,
                        "title_zh": title_zh,
                        "summary_zh": summary,
                        "premises_and_conditions": premises,
                        "ion_stage": pick("ion_stage", ""),
                        "isoelectronic": pick("isoelectronic", ""),
                        "z_num": pick("z_num"),
                        "method": pick("method", ""),
                        "code_name": pick("code_name", ""),
                        "physical_process": pick("physical_process", ""),
                        "keywords": _normalize_list(
                            pick("keywords", pick("tags", []))
                        ),
                    }
                )

            recency_year = _normalize_year(pick("recency_year")) or default_year
            card_year = _normalize_year(pick("card_year")) or default_year
            try:
                consensus = max(1, int(pick("consensus_count", 1) or 1))
            except (TypeError, ValueError):
                consensus = 1
            try:
                flags = int(pick("conclusion_flags", 0) or 0)
            except (TypeError, ValueError):
                flags = 0
            authority = pick("authority_score")
            if authority in (None, ""):
                authority = self._authority_for_docs(doc_ids)
            try:
                authority = float(authority)
            except (TypeError, ValueError):
                authority = 1.0
            try:
                z_pick = pick("z_num")
                if z_pick not in (None, ""):
                    z_int = int(z_pick)
                    z_num = z_int if 1 <= z_int <= 250 else None
                else:
                    z_num = None
            except (TypeError, ValueError, OverflowError):
                z_num = None
            try:
                page_num = int(pick("page_num")) if pick("page_num") not in (None, "") else None
            except (TypeError, ValueError):
                page_num = None
            try:
                user_verified = int(pick("is_user_verified", 0) or 0)
            except (TypeError, ValueError):
                user_verified = 0
            user_verified = 1 if user_verified else 0

            values = {
                "knw_id": knw_id,
                "card_level": level,
                "card_type": card_type,
                "element": _clean(pick("element", "")) or None,
                "z_num": z_num,
                "ion_stage": _clean(pick("ion_stage", "")) or None,
                "isoelectronic": _clean(pick("isoelectronic", "")) or None,
                "shell_type": _clean(pick("shell_type", "")) or None,
                "method": _clean(pick("method", "")) or None,
                "code_name": _clean(pick("code_name", "")) or None,
                "physical_process": _clean(pick("physical_process", "")) or None,
                "conclusion_flags": flags,
                "title_zh": title_zh,
                "title_en": title_en,
                "premises_and_conditions": premises,
                "summary_zh": summary,
                "structured_details_json": _json_dump(structured),
                "formulas_latex": formulas,
                "code_snippet": code_snippet,
                "pitfall_warning": pitfall,
                "search_payload": search_payload,
                "consensus_count": consensus,
                "authority_score": authority,
                "recency_year": recency_year,
                "card_year": card_year,
                "has_conflict": int(pick("has_conflict", 0) or 0),
                "conflict_with_knw_ids": _json_dump(conflicts),
                "supporting_doc_ids": _json_dump(doc_ids),
                "is_vectorized": int(pick("is_vectorized", 0) or 0),
                "is_user_verified": user_verified,
                "source_kind": _clean(pick("source_kind", "EXTRACTION")) or "EXTRACTION",
                "source_section": _clean(pick("source_section", "")),
                "page_num": page_num,
                "created_at": (
                    existing["created_at"]
                    if existing
                    else (data.get("created_at") or now_iso())
                ),
                "updated_at": now_iso(),
            }
            values["content_hash"] = compute_card_content_hash(values)
            self.conn.execute(
                """
                INSERT INTO knowledge_cards (
                    knw_id, card_level, card_type, element, z_num, ion_stage,
                    isoelectronic, shell_type, method, code_name, physical_process,
                    conclusion_flags, title_zh, title_en, premises_and_conditions,
                    summary_zh, structured_details_json, formulas_latex, code_snippet,
                    pitfall_warning, search_payload, consensus_count, authority_score,
                    recency_year, card_year, has_conflict, conflict_with_knw_ids,
                    supporting_doc_ids, is_vectorized, is_user_verified,
                    source_kind, content_hash, source_section, page_num,
                    created_at, updated_at
                ) VALUES (
                    :knw_id, :card_level, :card_type, :element, :z_num, :ion_stage,
                    :isoelectronic, :shell_type, :method, :code_name,
                    :physical_process, :conclusion_flags, :title_zh, :title_en,
                    :premises_and_conditions, :summary_zh, :structured_details_json,
                    :formulas_latex, :code_snippet, :pitfall_warning, :search_payload,
                    :consensus_count, :authority_score, :recency_year, :card_year,
                    :has_conflict, :conflict_with_knw_ids, :supporting_doc_ids,
                    :is_vectorized, :is_user_verified, :source_kind, :content_hash,
                    :source_section, :page_num, :created_at, :updated_at
                )
                ON CONFLICT(knw_id) DO UPDATE SET
                    card_level=excluded.card_level,
                    card_type=excluded.card_type,
                    element=excluded.element,
                    z_num=excluded.z_num,
                    ion_stage=excluded.ion_stage,
                    isoelectronic=excluded.isoelectronic,
                    shell_type=excluded.shell_type,
                    method=excluded.method,
                    code_name=excluded.code_name,
                    physical_process=excluded.physical_process,
                    conclusion_flags=excluded.conclusion_flags,
                    title_zh=excluded.title_zh,
                    title_en=excluded.title_en,
                    premises_and_conditions=excluded.premises_and_conditions,
                    summary_zh=excluded.summary_zh,
                    structured_details_json=excluded.structured_details_json,
                    formulas_latex=excluded.formulas_latex,
                    code_snippet=excluded.code_snippet,
                    pitfall_warning=excluded.pitfall_warning,
                    search_payload=excluded.search_payload,
                    consensus_count=excluded.consensus_count,
                    authority_score=excluded.authority_score,
                    recency_year=excluded.recency_year,
                    card_year=excluded.card_year,
                    has_conflict=excluded.has_conflict,
                    conflict_with_knw_ids=excluded.conflict_with_knw_ids,
                    supporting_doc_ids=excluded.supporting_doc_ids,
                    is_vectorized=excluded.is_vectorized,
                    is_user_verified=excluded.is_user_verified,
                    source_kind=excluded.source_kind,
                    content_hash=excluded.content_hash,
                    source_section=excluded.source_section,
                    page_num=excluded.page_num,
                    updated_at=excluded.updated_at
                """,
                values,
            )
            self._commit()
            all_affected_docs = set(doc_ids)
            if existing and existing.get("supporting_doc_ids"):
                try:
                    prev_ids = _load_json(existing["supporting_doc_ids"], [])
                    all_affected_docs.update(prev_ids)
                except Exception:
                    pass
            if all_affected_docs:
                self._refresh_document_pipeline_cache(all_affected_docs)
                self._commit()
            if existing is None:
                self.log_event(
                    "INFO",
                    "knowledge",
                    f"新增知识卡 {knw_id}: {title_zh}",
                    doc_id=doc_ids[0] if doc_ids else None,
                )
            return knw_id

    def _max_pub_year(self, doc_ids: Iterable[str]) -> int | None:
        ids = [str(item) for item in doc_ids if item]
        if not ids:
            return None
        placeholders = ",".join("?" for _ in ids)
        row = self.conn.execute(
            f"SELECT MAX(pub_year) FROM documents WHERE doc_id IN ({placeholders})",
            ids,
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else None

    def _authority_for_docs(self, doc_ids: Iterable[str]) -> float:
        ids = [str(item) for item in doc_ids if item]
        if not ids:
            return 1.0
        placeholders = ",".join("?" for _ in ids)
        row = self.conn.execute(
            f"SELECT MAX(authority_weight) FROM documents WHERE doc_id IN ({placeholders})",
            ids,
        ).fetchone()
        return float(row[0]) if row and row[0] is not None else 1.0

    def get_knowledge_card(self, knw_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM knowledge_cards WHERE knw_id = ?",
                (_clean(knw_id).upper(),),
            ).fetchone()
            if not row:
                return None
            result = self._card_dict(row)
            result["keywords"] = self.keywords_for_card(knw_id)
            result["evidence"] = self.list_evidence(knw_id)
            result["provenance"] = self.provenance_for_card(knw_id)
            result["audit_reviews"] = self.audit_reviews_for_card(knw_id)
            result["relations"] = self.card_relations_for_card(knw_id)
            return result

    # ---------------------------------------------------- v3.1 card relations
    def add_card_relation(
        self,
        source_knw_id: str,
        target_knw_id: str,
        relation_type: str,
        *,
        confidence: float = 0.0,
        reason: str = "",
        evidence: Mapping[str, Any] | None = None,
    ) -> str:
        """Create or refresh a non-destructive relation between two cards.

        Duplicate claims are kept as separate cards; the relation records why
        they are linked.  Contradictions are also kept as relations so audit
        can revisit them without losing either card.
        """
        source = _clean(source_knw_id).upper()
        target = _clean(target_knw_id).upper()
        kind = _clean(relation_type).upper()
        if not source or not target or source == target:
            return ""
        if kind not in {"SAME_CLAIM", "SUPPORTS", "CONTRADICTS", "RELATED"}:
            raise ValueError(f"未知卡片关系类型: {relation_type}")
        relation_id = "REL-" + hashlib.sha256(
            f"{source}|{target}|{kind}".encode("utf-8")
        ).hexdigest()[:12].upper()
        payload = _json_dump(dict(evidence or {}))
        try:
            score = float(confidence or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        score = max(0.0, min(1.0, score))
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO card_relations(
                    relation_id, source_knw_id, target_knw_id, relation_type,
                    confidence, reason, evidence_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(relation_id) DO UPDATE SET
                    confidence=excluded.confidence,
                    reason=excluded.reason,
                    evidence_json=excluded.evidence_json,
                    updated_at=excluded.updated_at
                """,
                (
                    relation_id,
                    source,
                    target,
                    kind,
                    score,
                    _clean(reason),
                    payload,
                    now_iso(),
                    now_iso(),
                ),
            )
            self._commit()
        return relation_id

    def card_relations_for_card(self, knw_id: str) -> list[dict[str, Any]]:
        wanted = _clean(knw_id).upper()
        if not wanted:
            return []
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT r.*,
                       CASE WHEN r.source_knw_id = ? THEN r.target_knw_id
                            ELSE r.source_knw_id END AS other_knw_id,
                       c.title_zh AS other_title_zh,
                       c.title_en AS other_title_en,
                       c.card_type AS other_card_type,
                       c.card_level AS other_card_level
                FROM card_relations r
                JOIN knowledge_cards c ON c.knw_id = CASE
                    WHEN r.source_knw_id = ? THEN r.target_knw_id
                    ELSE r.source_knw_id END
                WHERE r.source_knw_id = ? OR r.target_knw_id = ?
                ORDER BY r.relation_type, r.updated_at DESC
                """,
                (wanted, wanted, wanted, wanted),
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["direction"] = "outgoing" if item["source_knw_id"] == wanted else "incoming"
                result.append(item)
            return result

    # ---------------------------------------------------------- v3 audit data
    def set_card_provenance(
        self,
        knw_id: str,
        role: str,
        node: Mapping[str, Any] | None,
        *,
        source: str = "extraction",
    ) -> None:
        """Store the latest model/node used for one card role."""
        if not knw_id or not role:
            return
        info = dict(node or {})
        values = {
            "knw_id": _clean(knw_id).upper(),
            "role": _clean(role),
            "node_id": _clean(info.get("node_id")),
            "node_name": _clean(info.get("name") or info.get("node_name")),
            "node_type": _clean(info.get("node_type")),
            "provider": _clean(info.get("provider")),
            "model": _clean(info.get("model")),
            "web_preset": _clean(info.get("web_preset")),
            "web_model": _clean(info.get("web_model")),
            "source": _clean(source) or "extraction",
            "recorded_at": now_iso(),
        }
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO card_model_provenance (
                    knw_id, role, node_id, node_name, node_type, provider,
                    model, web_preset, web_model, source, recorded_at
                ) VALUES (
                    :knw_id, :role, :node_id, :node_name, :node_type, :provider,
                    :model, :web_preset, :web_model, :source, :recorded_at
                )
                ON CONFLICT(knw_id, role) DO UPDATE SET
                    node_id=excluded.node_id,
                    node_name=excluded.node_name,
                    node_type=excluded.node_type,
                    provider=excluded.provider,
                    model=excluded.model,
                    web_preset=excluded.web_preset,
                    web_model=excluded.web_model,
                    source=excluded.source,
                    recorded_at=excluded.recorded_at
                """,
                values,
            )
            self._commit()

    def provenance_for_card(self, knw_id: str) -> dict[str, dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM card_model_provenance WHERE knw_id = ?",
                (_clean(knw_id).upper(),),
            ).fetchall()
        return {str(row["role"]): dict(row) for row in rows}

    def provenance_for_cards(
        self, knw_ids: Iterable[str]
    ) -> dict[str, dict[str, dict[str, Any]]]:
        ids = [str(item).upper() for item in knw_ids if item]
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        with self._lock:
            rows = self.conn.execute(
                f"SELECT * FROM card_model_provenance "
                f"WHERE knw_id IN ({placeholders})",
                ids,
            ).fetchall()
        grouped: dict[str, dict[str, dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(str(row["knw_id"]), {})[str(row["role"])] = dict(row)
        return grouped

    def record_audit_review(self, review: Mapping[str, Any]) -> str:
        """Insert/update one external audit review idempotently."""
        data = dict(review)
        report_hash = _clean(data.get("report_hash"))
        knw_id = _clean(data.get("knw_id")).upper()
        auditor = _clean(data.get("auditor_name")) or "未命名审计 AI"
        review_id = _clean(data.get("review_id"))
        if not review_id:
            seed = f"{report_hash}|{knw_id}|{auditor}".encode("utf-8")
            review_id = "REV-" + hashlib.sha256(seed).hexdigest()[:24]
        score = data.get("overall_score")
        try:
            score = int(score) if score is not None else None
        except (TypeError, ValueError):
            score = None
        confidence = data.get("confidence")
        try:
            confidence = float(confidence) if confidence is not None else None
        except (TypeError, ValueError):
            confidence = None
        values = {
            "review_id": review_id,
            "knw_id": knw_id,
            "doc_id": _clean(data.get("doc_id")).upper(),
            "report_id": _clean(data.get("report_id")),
            "report_hash": report_hash,
            "batch_id": _clean(data.get("batch_id")),
            "auditor_name": auditor,
            "auditor_model": _clean(data.get("auditor_model")),
            "auditor_provider": _clean(data.get("auditor_provider")),
            "auditor_node_id": _clean(data.get("auditor_node_id")),
            "audit_source": _clean(data.get("audit_source")) or "manual_import",
            "overall_score": score,
            "verdict": _clean(data.get("verdict")),
            "confidence": confidence,
            "needs_manual_review": 1 if data.get("needs_manual_review") else 0,
            "suggested_action": _clean(data.get("suggested_action")) or "keep",
            "issues_json": _json_dump(data.get("issues") or []),
            "revised_card_json": _json_dump(data.get("revised_card") or {}),
            "delete_reason": _clean(data.get("delete_reason")),
            "raw_json": _json_dump(data.get("raw_json") or data),
            "imported_at": _clean(data.get("imported_at")) or now_iso(),
            "applied_at": _clean(data.get("applied_at")) or None,
            "status": _clean(data.get("status")) or "IMPORTED",
            "validation_warning": _clean(data.get("validation_warning")),
        }
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO card_audit_reviews (
                    review_id, knw_id, doc_id, report_id, report_hash, batch_id,
                    auditor_name, auditor_model, auditor_provider, auditor_node_id,
                    audit_source, overall_score, verdict, confidence,
                    needs_manual_review, suggested_action, issues_json,
                    revised_card_json, delete_reason, raw_json, imported_at,
                    applied_at, status, validation_warning
                ) VALUES (
                    :review_id, :knw_id, :doc_id, :report_id, :report_hash,
                    :batch_id, :auditor_name, :auditor_model, :auditor_provider,
                    :auditor_node_id, :audit_source, :overall_score, :verdict,
                    :confidence, :needs_manual_review, :suggested_action,
                    :issues_json, :revised_card_json, :delete_reason, :raw_json,
                    :imported_at, :applied_at, :status, :validation_warning
                )
                ON CONFLICT(report_hash, knw_id, auditor_name) DO UPDATE SET
                    review_id=excluded.review_id,
                    doc_id=excluded.doc_id,
                    report_id=excluded.report_id,
                    batch_id=excluded.batch_id,
                    auditor_model=excluded.auditor_model,
                    auditor_provider=excluded.auditor_provider,
                    auditor_node_id=excluded.auditor_node_id,
                    audit_source=excluded.audit_source,
                    overall_score=excluded.overall_score,
                    verdict=excluded.verdict,
                    confidence=excluded.confidence,
                    needs_manual_review=excluded.needs_manual_review,
                    suggested_action=excluded.suggested_action,
                    issues_json=excluded.issues_json,
                    revised_card_json=excluded.revised_card_json,
                    delete_reason=excluded.delete_reason,
                    raw_json=excluded.raw_json,
                    applied_at=excluded.applied_at,
                    status=excluded.status,
                    validation_warning=excluded.validation_warning
                """,
                values,
            )
            self._commit()
        return review_id

    def audit_reviews_for_card(
        self, knw_id: str, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM card_audit_reviews WHERE knw_id = ? "
                "ORDER BY imported_at DESC, review_id LIMIT ?",
                (_clean(knw_id).upper(), max(1, min(int(limit), 500))),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["issues"] = _load_json(item.get("issues_json"), [])
            item["revised_card"] = _load_json(item.get("revised_card_json"), {})
            item["raw"] = _load_json(item.get("raw_json"), {})
            result.append(item)
        return result

    def refresh_audit_summary(self, knw_id: str) -> dict[str, int]:
        """Recompute the denormalized multi-AI counts for one card."""
        wanted = _clean(knw_id).upper()
        with self._lock:
            rows = self.conn.execute(
                "SELECT overall_score, suggested_action, status "
                "FROM card_audit_reviews WHERE knw_id = ? "
                "AND status NOT IN ('REJECTED', 'STALE')",
                (wanted,),
            ).fetchall()
            scores = [
                int(row["overall_score"])
                for row in rows
                if row["overall_score"] is not None
            ]
            positive = sum(1 for score in scores if score >= 70)
            negative = sum(1 for score in scores if score < 50)
            score_min = min(scores) if scores else None
            score_max = max(scores) if scores else None
            divergence = 1 if (
                (score_min is not None and score_max is not None
                 and score_max - score_min >= 30)
                or (positive > 0 and negative > 0)
            ) else 0
            self.conn.execute(
                """
                UPDATE knowledge_cards SET
                    audit_review_count = ?,
                    audit_positive_count = ?,
                    audit_negative_count = ?,
                    audit_score_min = ?,
                    audit_score_max = ?,
                    audit_divergence = ?
                WHERE knw_id = ?
                """,
                (
                    len(rows),
                    positive,
                    negative,
                    score_min,
                    score_max,
                    divergence,
                    wanted,
                ),
            )
            self._commit()
        return {
            "review_count": len(rows),
            "positive": positive,
            "negative": negative,
            "min": score_min if score_min is not None else -1,
            "max": score_max if score_max is not None else -1,
            "divergence": divergence,
        }

    def mark_card_suggested_delete(
        self,
        knw_id: str,
        *,
        reason: str,
        auditor: str,
        hide: bool = True,
    ) -> None:
        with self._lock:
            self.conn.execute(
                """
                UPDATE knowledge_cards SET
                    audit_status='SUGGESTED_DELETE',
                    audit_delete_suggested=1,
                    audit_hidden=?,
                    audit_delete_reason=?,
                    audit_delete_by=?,
                    audit_delete_at=?,
                    updated_at=?
                WHERE knw_id=?
                """,
                (
                    1 if hide else 0,
                    _clean(reason),
                    _clean(auditor),
                    now_iso(),
                    now_iso(),
                    _clean(knw_id).upper(),
                ),
            )
            self._commit()

    def restore_card_from_audit_suggestion(self, knw_id: str) -> bool:
        wanted = _clean(knw_id).upper()
        with self._lock:
            cursor = self.conn.execute(
                """
                UPDATE knowledge_cards SET
                    audit_status='NORMAL',
                    audit_delete_suggested=0,
                    audit_hidden=0,
                    audit_delete_reason='',
                    audit_delete_by='',
                    audit_delete_at=NULL,
                    needs_vector_update=1,
                    updated_at=?
                WHERE knw_id=? AND audit_delete_suggested=1
                """,
                (now_iso(), wanted),
            )
            self._commit()
            restored = cursor.rowcount > 0
        if restored:
            self.enqueue_vector_tasks([wanted])
            self.log_event(
                "INFO",
                "audit",
                f"用户恢复被审计建议删除的卡片 {wanted}",
            )
        return restored

    def set_card_audit_status(
        self,
        knw_id: str,
        status: str,
        *,
        hidden: bool | None = None,
    ) -> None:
        sets = ["audit_status=?", "updated_at=?"]
        params: list[Any] = [status.upper(), now_iso()]
        if hidden is not None:
            sets.append("audit_hidden=?")
            params.append(1 if hidden else 0)
        params.append(_clean(knw_id).upper())
        with self._lock:
            self.conn.execute(
                f"UPDATE knowledge_cards SET {', '.join(sets)} WHERE knw_id=?",
                params,
            )
            self._commit()

    def add_card_revision(
        self,
        knw_id: str,
        *,
        audit_review_id: str,
        before: Mapping[str, Any],
        after: Mapping[str, Any],
        applied_by: str,
    ) -> None:
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO card_revisions (
                    knw_id, audit_review_id, before_json, after_json,
                    created_at, applied_by
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    _clean(knw_id).upper(),
                    _clean(audit_review_id),
                    _json_dump(before),
                    _json_dump(after),
                    now_iso(),
                    _clean(applied_by),
                ),
            )
            self._commit()

    def card_revisions_for_card(
        self, knw_id: str, *, limit: int = 20
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM card_revisions WHERE knw_id = ? "
                "ORDER BY created_at DESC, revision_id DESC LIMIT ?",
                (_clean(knw_id).upper(), max(1, min(int(limit), 100))),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["before"] = _load_json(item.get("before_json"), {})
            item["after"] = _load_json(item.get("after_json"), {})
            result.append(item)
        return result

    def mark_cards_vector_pending(self, knw_ids: Iterable[str]) -> None:
        ids = [str(item).upper() for item in knw_ids if item]
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        with self._lock:
            self.conn.execute(
                f"UPDATE knowledge_cards SET needs_vector_update=1 "
                f"WHERE knw_id IN ({placeholders})",
                ids,
            )
            self._commit()

    def record_audit_export(
        self,
        *,
        report_id: str,
        doc_id: str,
        report_hash: str,
        card_count: int,
        part_count: int,
        export_dir: str,
        manifest: Mapping[str, Any],
    ) -> None:
        with self._lock:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO document_audit_exports (
                    report_id, doc_id, report_hash, schema, card_count,
                    part_count, exported_at, export_dir, manifest_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _clean(report_id),
                    _clean(doc_id).upper(),
                    _clean(report_hash),
                    "acdb.document_audit_report/v1",
                    int(card_count),
                    int(part_count),
                    now_iso(),
                    str(export_dir),
                    _json_dump(manifest),
                ),
            )
            self._commit()

    def audit_export_for_report(self, report_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM document_audit_exports WHERE report_id = ?",
                (_clean(report_id),),
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["manifest"] = _load_json(item.get("manifest_json"), {})
        return item

    def list_audit_exports(
        self, *, doc_id: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        with self._lock:
            if doc_id:
                rows = self.conn.execute(
                    """
                    SELECT * FROM document_audit_exports
                    WHERE doc_id = ?
                    ORDER BY exported_at DESC
                    LIMIT ?
                    """,
                    (_clean(doc_id).upper(), max(1, int(limit))),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    """
                    SELECT * FROM document_audit_exports
                    ORDER BY exported_at DESC
                    LIMIT ?
                    """,
                    (max(1, int(limit)),),
                ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["manifest"] = _load_json(item.get("manifest_json"), {})
            items.append(item)
        return items

    def record_audit_import(
        self,
        *,
        batch_id: str,
        report_id: str,
        report_hash: str,
        auditor_name: str,
        file_name: str,
        file_hash: str,
        stats: Mapping[str, Any],
        applied: bool = False,
    ) -> None:
        with self._lock:
            self.conn.execute(
                """
                INSERT OR IGNORE INTO audit_import_batches (
                    batch_id, report_id, report_hash, auditor_name,
                    file_name, file_hash, imported_at, applied_at, stats_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _clean(batch_id),
                    _clean(report_id),
                    _clean(report_hash),
                    _clean(auditor_name),
                    _clean(file_name),
                    _clean(file_hash),
                    now_iso(),
                    now_iso() if applied else None,
                    _json_dump(stats),
                ),
            )
            self._commit()

    def list_knowledge_cards(
        self,
        *,
        filters: Mapping[str, Any] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses, params = self._card_filter_sql(filters or {}, alias="c")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend([max(1, min(int(limit), 1000)), max(0, int(offset))])
        sql = f"""
            SELECT c.* FROM knowledge_cards c
            {where}
            ORDER BY CASE WHEN c.card_level='L1' THEN 0 ELSE 1 END,
                     c.authority_score DESC, c.consensus_count DESC,
                     c.recency_year DESC, c.updated_at DESC
            LIMIT ? OFFSET ?
        """
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
            return [self._card_dict(row) for row in rows]

    def get_knowledge_cards(self, knw_ids: Iterable[str]) -> list[dict[str, Any]]:
        ids = [str(item).upper() for item in knw_ids if item]
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        with self._lock:
            rows = self.conn.execute(
                f"SELECT * FROM knowledge_cards WHERE knw_id IN ({placeholders})",
                ids,
            ).fetchall()
            by_id = {row["knw_id"]: self._card_dict(row) for row in rows}
            return [by_id[knw_id] for knw_id in ids if knw_id in by_id]

    def count_knowledge_cards(self, filters: Mapping[str, Any] | None = None) -> int:
        clauses, params = self._card_filter_sql(filters or {}, alias="c")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            return int(
                self.conn.execute(
                    f"SELECT COUNT(*) FROM knowledge_cards c {where}", params
                ).fetchone()[0]
            )

    def conflict_cards(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM knowledge_cards WHERE has_conflict = 1 "
                "ORDER BY updated_at DESC LIMIT ?",
                (max(1, min(limit, 200)),),
            ).fetchall()
            return [self._card_dict(row) for row in rows]

    def mark_vectorized(self, knw_ids: Iterable[str], flag: bool = True) -> None:
        ids = [str(item).upper() for item in knw_ids if item]
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        with self._lock:
            self.conn.execute(
                f"UPDATE knowledge_cards SET is_vectorized=?, "
                f"needs_vector_update=? WHERE knw_id IN ({placeholders})",
                [1 if flag else 0, 0 if flag else 1, *ids],
            )
            self._commit()

    def find_duplicate_card(
        self,
        doc_id: str,
        *,
        title_zh: str = "",
        summary_zh: str = "",
    ) -> dict[str, Any] | None:
        """Find a same-document card with identical title or summary."""
        wanted_doc = _clean(doc_id).upper()
        if not wanted_doc:
            return None
        title = _clean(title_zh)
        summary = _clean(summary_zh)
        if not title and not summary:
            return None
        clauses = ["UPPER(supporting_doc_ids) LIKE ?"]
        params: list[Any] = [f'%"{wanted_doc}"%']
        if title:
            clauses.append("title_zh = ?")
            params.append(title)
        elif summary:
            clauses.append("summary_zh = ?")
            params.append(summary)
        with self._lock:
            row = self.conn.execute(
                f"SELECT * FROM knowledge_cards WHERE {' AND '.join(clauses)} "
                "ORDER BY updated_at DESC LIMIT 1",
                params,
            ).fetchone()
        return self._card_dict(row) if row else None

    def delete_knowledge_card(self, knw_id: str) -> bool:
        with self._lock:
            card_id = _clean(knw_id).upper()
            row = self.conn.execute(
                "SELECT supporting_doc_ids FROM knowledge_cards WHERE knw_id = ?",
                (card_id,),
            ).fetchone()
            doc_ids: list[str] = []
            if row and row["supporting_doc_ids"]:
                try:
                    doc_ids = _load_json(row["supporting_doc_ids"], [])
                except Exception:
                    pass
            cursor = self.conn.execute(
                "DELETE FROM knowledge_cards WHERE knw_id = ?",
                (card_id,),
            )
            self._commit()
            if cursor.rowcount > 0 and doc_ids:
                self._refresh_document_pipeline_cache(doc_ids)
                self._commit()
            return cursor.rowcount > 0

    def cards_for_document(self, doc_id: str) -> list[dict[str, Any]]:
        wanted = _clean(doc_id).upper()
        if not wanted:
            return []
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM knowledge_cards WHERE UPPER(supporting_doc_ids) LIKE ? ORDER BY updated_at DESC",
                (f'%"{wanted}"%',),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            card = self._card_dict(row)
            if wanted in (card.get("supporting_doc_ids_list") or []):
                result.append(card)
        return result

    def cards_summary_for_all_documents(self) -> dict[str, dict[str, Any]]:
        """Return {doc_id: {'card_count': int, 'has_master': bool}} in a single fast pass."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT card_level, card_type, supporting_doc_ids FROM knowledge_cards"
            ).fetchall()
        summary: dict[str, dict[str, Any]] = {}
        for row in rows:
            level = str(row["card_level"] or "L2").upper()
            ctype = str(row["card_type"] or "").upper()
            is_master = (level == "L1" or ctype == "MASTER")
            doc_ids = _load_json(row["supporting_doc_ids"], []) or []
            for did in doc_ids:
                did_clean = str(did).strip().upper()
                if not did_clean:
                    continue
                entry = summary.get(did_clean)
                if entry is None:
                    entry = {"card_count": 0, "has_master": False}
                    summary[did_clean] = entry
                entry["card_count"] += 1
                if is_master:
                    entry["has_master"] = True
        return summary

    def delete_unverified_cards_for_document(self, doc_id: str) -> dict[str, Any]:
        """Remove auto-generated cards for a document, keeping user-verified ones."""
        deleted: list[str] = []
        kept: list[str] = []
        for card in self.cards_for_document(doc_id):
            knw_id = str(card.get("knw_id") or "")
            if not knw_id:
                continue
            if int(card.get("is_user_verified") or 0):
                kept.append(knw_id)
                continue
            if self.delete_knowledge_card(knw_id):
                deleted.append(knw_id)
        return {"deleted": deleted, "kept_verified": kept}

    def has_bundle_card(self, doc_id: str, bundle_hash: str) -> bool:
        if not bundle_hash:
            return False
        with self._lock:
            row = self.conn.execute(
                """
                SELECT 1
                FROM raw_evidence_chunks e
                JOIN knowledge_cards c ON c.knw_id = e.parent_knw_id
                WHERE e.doc_id = ? AND e.bundle_hash = ?
                LIMIT 1
                """,
                (_clean(doc_id).upper(), str(bundle_hash)),
            ).fetchone()
            return row is not None

    def bundle_hashes_for_document(self, doc_id: str) -> set[str]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT DISTINCT bundle_hash FROM raw_evidence_chunks "
                "WHERE doc_id = ? AND bundle_hash != ''",
                (_clean(doc_id).upper(),),
            ).fetchall()
            return {str(row["bundle_hash"]) for row in rows if row["bundle_hash"]}

    def backfill_bundle_hashes(self, hasher: Any) -> int:
        """Backfill legacy evidence rows so incremental mode can skip them."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT chunk_id, context_bundle_md FROM raw_evidence_chunks "
                "WHERE bundle_hash IS NULL OR bundle_hash = ''"
            ).fetchall()
            updated = 0
            for row in rows:
                value = hasher(row["context_bundle_md"])
                if not value:
                    continue
                self.conn.execute(
                    "UPDATE raw_evidence_chunks SET bundle_hash=? WHERE chunk_id=?",
                    (str(value), row["chunk_id"]),
                )
                updated += 1
            if updated:
                self._commit()
            return updated

    def _card_filter_sql(
        self, filters: Mapping[str, Any], *, alias: str = "c"
    ) -> tuple[list[str], list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        prefix = f"{alias}." if alias else ""

        exact_fields = (
            "card_level",
            "card_type",
            "code_name",
            "shell_type",
            "ion_stage",
        )
        for field in exact_fields:
            value = filters.get(field)
            if value not in (None, ""):
                if field in ("card_level", "card_type"):
                    clauses.append(f"{prefix}{field} = ?")
                    params.append(str(value).upper())
                else:
                    clauses.append(f"LOWER({prefix}{field}) = LOWER(?)")
                    params.append(str(value))
        if filters.get("element"):
            clauses.append(f"UPPER({prefix}element) = UPPER(?)")
            params.append(str(filters["element"]))
        z_filter = filters.get("z_num")
        if z_filter not in (None, ""):
            try:
                z_int = int(z_filter)
                if 1 <= z_int <= 250:
                    clauses.append(f"{prefix}z_num = ?")
                    params.append(z_int)
            except (ValueError, TypeError, OverflowError):
                pass
        if filters.get("isoelectronic"):
            clauses.append(f"LOWER({prefix}isoelectronic) = LOWER(?)")
            params.append(str(filters["isoelectronic"]))
        if filters.get("method"):
            clauses.append(f"{prefix}method LIKE ?")
            params.append(f"%{filters['method']}%")
        if filters.get("physical_process"):
            clauses.append(f"{prefix}physical_process LIKE ?")
            params.append(f"%{filters['physical_process']}%")
        if filters.get("has_conflict") not in (None, ""):
            clauses.append(f"{prefix}has_conflict = ?")
            params.append(1 if filters["has_conflict"] else 0)
        if filters.get("conclusion_flags") not in (None, "", 0):
            clauses.append(f"({prefix}conclusion_flags & ?) <> 0")
            params.append(int(filters["conclusion_flags"]))
        for field in ("audit_hidden", "audit_delete_suggested", "audit_divergence"):
            value = filters.get(field)
            if value not in (None, ""):
                clauses.append(f"{prefix}{field} = ?")
                params.append(1 if value else 0)
        if filters.get("audit_status"):
            clauses.append(f"{prefix}audit_status = ?")
            params.append(str(filters["audit_status"]).upper())
        if filters.get("source_kind"):
            clauses.append(f"{prefix}source_kind = ?")
            params.append(str(filters["source_kind"]).upper())
        return clauses, params

    # ------------------------------------------------------------------- FTS
    def search_cards_fts(
        self,
        query: str,
        *,
        filters: Mapping[str, Any] | None = None,
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        """BM25 recall over knowledge_fts, with a CJK LIKE fallback."""
        text = _clean(query)
        clauses, params = self._card_filter_sql(filters or {}, alias="c")
        if not text:
            return self.list_knowledge_cards(filters=filters, limit=limit)

        match_query = _fts_query(text)
        results: dict[str, dict[str, Any]] = {}
        if match_query:
            where = ["knowledge_fts MATCH ?", *clauses]
            sql = f"""
                SELECT c.*, snippet(knowledge_fts, 2, ?, ?, ' … ', 30) AS snippet,
                       bm25(knowledge_fts, 0.0, 4.0, 5.0, 2.0, 1.5) AS bm25_score
                FROM knowledge_fts
                JOIN knowledge_cards c ON c.knw_id = knowledge_fts.knw_id
                WHERE {' AND '.join(where)}
                ORDER BY bm25_score LIMIT ?
            """
            marker_params = ["\x01", "\x02", match_query, *params, max(1, min(limit, 5000))]
            with self._lock:
                rows = self.conn.execute(sql, marker_params).fetchall()
            for row in rows:
                item = self._card_dict(row)
                item["snippet"] = row["snippet"] or item["search_payload"][:200]
                item["bm25_score"] = float(row["bm25_score"])
                results[item["knw_id"]] = item

        if _CJK_RE.search(text) or not match_query:
            for item in self._like_card_search(text, filters=filters, limit=limit):
                results.setdefault(item["knw_id"], item)
        return sorted(
            results.values(),
            key=lambda item: item.get("bm25_score", -0.5),
        )[:limit]

    def _like_card_search(
        self,
        query: str,
        *,
        filters: Mapping[str, Any] | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        terms = _CJK_RE.findall(query) or ([query] if query else [])
        terms = list(dict.fromkeys(term for term in terms if term))[:6]
        if not terms:
            return []
        clauses, params = self._card_filter_sql(filters or {}, alias="c")
        results: dict[str, dict[str, Any]] = {}
        for term in terms:
            pattern = _like_pattern(term)
            where = [
                "(c.title_zh LIKE ? ESCAPE '\\' OR c.search_payload LIKE ? ESCAPE '\\' "
                "OR c.formulas_latex LIKE ? ESCAPE '\\' OR c.code_snippet LIKE ? ESCAPE '\\')",
                *clauses,
            ]
            sql = f"SELECT c.* FROM knowledge_cards c WHERE {' AND '.join(where)} LIMIT ?"
            with self._lock:
                rows = self.conn.execute(
                    sql, [pattern, pattern, pattern, pattern, *params, max(1, limit)]
                ).fetchall()
            for row in rows:
                item = self._card_dict(row)
                item["snippet"] = _manual_snippet(item["search_payload"], term)
                item["bm25_score"] = -0.5
                results.setdefault(item["knw_id"], item)
        return list(results.values())

    def search_documents(self, query: str, limit: int = 30) -> list[dict[str, Any]]:
        text = _clean(query)
        if not text:
            return []
        pattern = _like_pattern(text)
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT * FROM documents
                WHERE title LIKE ? ESCAPE '\\' OR abstract LIKE ? ESCAPE '\\'
                   OR authors LIKE ? ESCAPE '\\' OR journal LIKE ? ESCAPE '\\'
                ORDER BY updated_at DESC LIMIT ?
                """,
                (pattern, pattern, pattern, pattern, max(1, min(limit, 200))),
            ).fetchall()
            return [self._document_dict(row) for row in rows]

    def rebuild_fts(self) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO knowledge_fts(knowledge_fts) VALUES('rebuild')"
            )
            self.conn.execute(
                "INSERT INTO system_meta(key, value, updated_at) "
                "VALUES ('fts_rebuilt_v2','1',?) "
                "ON CONFLICT(key) DO UPDATE SET value='1', updated_at=excluded.updated_at",
                (now_iso(),),
            )
            self._commit()

    # --------------------------------------------------------------- keywords
    def upsert_keyword(
        self,
        term_en: str,
        *,
        term_zh: str = "",
        synonyms: Any = None,
        category: str = "",
        kw_id: str | None = None,
    ) -> str:
        term = _clean(term_en)
        if not term:
            raise ValueError("关键词不能为空")
        with self._lock:
            existing = self.conn.execute(
                "SELECT * FROM controlled_keywords WHERE term_en = ? COLLATE NOCASE",
                (term,),
            ).fetchone()
            if existing and not kw_id:
                kw_id = existing["kw_id"]
            kw_id = _clean(kw_id).upper() or generate_keyword_id(term)
            if classify_id(kw_id) is None or classify_id(kw_id)["family"] != "keyword":
                raise IdError(f"非法 KW-ID: {kw_id}")
            merged_zh = _clean(term_zh) or (existing["term_zh"] if existing else "")
            merged_cat = _clean(category) or (existing["category"] if existing else "")
            if synonyms is None:
                merged_syn = existing["synonyms_json"] if existing else "[]"
            else:
                merged_syn = _json_dump(_normalize_list(synonyms))
            self.conn.execute(
                """
                INSERT INTO controlled_keywords(
                    kw_id, term_en, term_zh, synonyms_json, category, hit_count,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(kw_id) DO UPDATE SET
                    term_en=excluded.term_en,
                    term_zh=excluded.term_zh,
                    synonyms_json=excluded.synonyms_json,
                    category=excluded.category,
                    updated_at=excluded.updated_at
                """,
                (
                    kw_id,
                    term,
                    merged_zh,
                    merged_syn,
                    merged_cat,
                    existing["created_at"] if existing else now_iso(),
                    now_iso(),
                ),
            )
            self._commit()
            return kw_id

    def get_keyword(self, kw_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM controlled_keywords WHERE kw_id = ?",
                (_clean(kw_id).upper(),),
            ).fetchone()
            return self._keyword_dict(row) if row else None

    def list_keywords(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT k.*, COUNT(ck.knw_id) AS card_count
                FROM controlled_keywords k
                LEFT JOIN card_keywords ck ON ck.kw_id = k.kw_id
                GROUP BY k.kw_id
                ORDER BY card_count DESC, k.hit_count DESC, k.term_en
                LIMIT ?
                """,
                (max(1, min(limit, 2000)),),
            ).fetchall()
            return [self._keyword_dict(row) for row in rows]

    def increment_keyword_hits(self, kw_id: str, amount: int = 1) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE controlled_keywords SET hit_count = hit_count + ?, "
                "updated_at = ? WHERE kw_id = ?",
                (int(amount), now_iso(), kw_id.upper()),
            )
            self._commit()

    def link_card_keywords(
        self, knw_id: str, keywords: Iterable[Any], *, weight: float = 1.0
    ) -> list[str]:
        linked: list[str] = []
        with self._lock:
            for item in keywords:
                if isinstance(item, Mapping):
                    term = item.get("term_en") or item.get("term") or ""
                    term_zh = item.get("term_zh", "")
                    synonyms = item.get("synonyms")
                    category = item.get("category", "")
                    kw_id = item.get("kw_id")
                else:
                    text = str(item).strip()
                    parsed = classify_id(text)
                    if parsed and parsed["family"] == "keyword":
                        existing_kw = self.get_keyword(text)
                        if existing_kw is None:
                            continue
                        kw_id = existing_kw["kw_id"]
                        term = existing_kw["term_en"]
                        term_zh = existing_kw.get("term_zh", "")
                        synonyms = existing_kw.get("synonyms_list")
                        category = existing_kw.get("category", "")
                    else:
                        term = text
                        term_zh = ""
                        synonyms = None
                        category = ""
                        kw_id = None
                if not term:
                    continue
                kw_id = self.upsert_keyword(
                    term,
                    term_zh=term_zh,
                    synonyms=synonyms,
                    category=category,
                    kw_id=kw_id,
                )
                self.conn.execute(
                    "INSERT INTO card_keywords(knw_id, kw_id, weight) VALUES (?, ?, ?) "
                    "ON CONFLICT(knw_id, kw_id) DO UPDATE SET weight=excluded.weight",
                    (knw_id.upper(), kw_id, float(weight)),
                )
                self.increment_keyword_hits(kw_id)
                linked.append(kw_id)
            self._commit()
        return linked

    def cards_for_keyword(self, kw_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT c.*, ck.weight FROM card_keywords ck
                JOIN knowledge_cards c ON c.knw_id = ck.knw_id
                WHERE ck.kw_id = ?
                ORDER BY c.authority_score DESC, c.updated_at DESC LIMIT ?
                """,
                (kw_id.upper(), max(1, min(limit, 500))),
            ).fetchall()
            return [self._card_dict(row) for row in rows]

    def all_card_keyword_links(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM card_keywords ORDER BY knw_id, kw_id"
            ).fetchall()
            return [dict(row) for row in rows]

    def keywords_for_card(self, knw_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT k.*, ck.weight FROM card_keywords ck
                JOIN controlled_keywords k ON k.kw_id = ck.kw_id
                WHERE ck.knw_id = ? ORDER BY ck.weight DESC, k.term_en
                """,
                (knw_id.upper(),),
            ).fetchall()
            return [self._keyword_dict(row) for row in rows]

    # --------------------------------------------------------------- evidence
    def add_evidence(
        self,
        parent_knw_id: str,
        doc_id: str,
        context_bundle_md: str,
        *,
        section_title: str = "",
        page_num: int | None = None,
        chunk_id: str | None = None,
        bundle_hash: str = "",
        locator: Mapping[str, Any] | None = None,
    ) -> str:
        text = str(context_bundle_md or "").strip()
        if not text:
            raise ValueError("evidence context cannot be empty")
        if not bundle_hash:
            canonical = re.sub(r"\s+", " ", text).strip().lower()
            bundle_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        chunk_id = _clean(chunk_id).upper() or generate_evidence_id(
            parent_knw_id, doc_id, page_num, text
        )
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO raw_evidence_chunks(
                    chunk_id, parent_knw_id, doc_id, section_title, page_num,
                    context_bundle_md, bundle_hash, locator_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chunk_id) DO UPDATE SET
                    parent_knw_id=excluded.parent_knw_id,
                    doc_id=excluded.doc_id,
                    section_title=excluded.section_title,
                    page_num=excluded.page_num,
                    context_bundle_md=excluded.context_bundle_md,
                    bundle_hash=excluded.bundle_hash,
                    locator_json=excluded.locator_json
                """,
                (
                    chunk_id,
                    parent_knw_id.upper(),
                    doc_id.upper(),
                    section_title,
                    page_num,
                    text,
                    str(bundle_hash or ""),
                    _json_dump(dict(locator or {})),
                    now_iso(),
                ),
            )
            self._commit()
        return chunk_id

    def list_evidence(self, knw_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM raw_evidence_chunks WHERE parent_knw_id = ? "
                "ORDER BY page_num, chunk_id",
                (knw_id.upper(),),
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["locator"] = _load_json(item.get("locator_json"), {})
                result.append(item)
            return result

    def get_evidence(self, chunk_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM raw_evidence_chunks WHERE chunk_id = ?",
                (chunk_id.upper(),),
            ).fetchone()
            if not row:
                return None
            item = dict(row)
            item["locator"] = _load_json(item.get("locator_json"), {})
            return item

    # --------------------------------------------------------------- notation
    def upsert_notation(self, raw_symbol: str, canonical_symbol: str, category: str = "") -> None:
        raw = _clean(raw_symbol)
        if not raw:
            raise ValueError("raw_symbol cannot be empty")
        with self._lock:
            self.conn.execute(
                "INSERT INTO notation_map(raw_symbol, canonical_symbol, category) "
                "VALUES (?, ?, ?) ON CONFLICT(raw_symbol) DO UPDATE SET "
                "canonical_symbol=excluded.canonical_symbol, category=excluded.category",
                (raw, _clean(canonical_symbol), _clean(category)),
            )
            self._commit()

    def normalize_symbol(self, raw_symbol: str) -> str:
        with self._lock:
            row = self.conn.execute(
                "SELECT canonical_symbol FROM notation_map WHERE raw_symbol = ?",
                (_clean(raw_symbol),),
            ).fetchone()
            return row["canonical_symbol"] if row else _clean(raw_symbol)

    def list_notations(self, limit: int = 500) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM notation_map ORDER BY category, raw_symbol LIMIT ?",
                (max(1, min(limit, 2000)),),
            ).fetchall()
            return [dict(row) for row in rows]

    # ------------------------------------------------------------------ tasks
    def has_pending_vector_task(self, knw_id: str) -> bool:
        wanted = _clean(knw_id).upper()
        if not wanted:
            return False
        with self._lock:
            row = self.conn.execute(
                """
                SELECT 1 FROM tasks
                WHERE task_type='VECTORIZE'
                  AND status='PENDING'
                  AND payload_json LIKE ?
                LIMIT 1
                """,
                (f'%"knw_id":"{wanted}"%',),
            ).fetchone()
            return row is not None

    def enqueue_vector_tasks(
        self,
        knw_ids: Iterable[str],
        *,
        doc_id: str | None = None,
        delay_sec: int = 0,
    ) -> int:
        """Queue idempotent Qdrant refresh jobs for cards.

        SQLite remains authoritative.  A failed Qdrant update is retried by a
        VECTORIZE task instead of being silently forgotten.
        """
        ids = [str(item).upper() for item in knw_ids if str(item or "").strip()]
        if not ids:
            return 0
        scheduled = (
            datetime.now().astimezone() + timedelta(seconds=max(0, int(delay_sec)))
        ).isoformat(timespec="seconds")
        queued = 0
        for knw_id in dict.fromkeys(ids):
            if self.has_pending_vector_task(knw_id):
                continue
            self.add_task(
                "VECTORIZE",
                doc_id=doc_id,
                payload={"knw_id": knw_id},
                priority=2,
                scheduled_for=scheduled if delay_sec else None,
            )
            queued += 1
        return queued

    def add_task(
        self,
        task_type: str,
        *,
        doc_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
        priority: int = 0,
        scheduled_for: str | None = None,
    ) -> int:
        with self._lock:
            cursor = self.conn.execute(
                """
                INSERT INTO tasks(task_type, doc_id, payload_json, status, priority,
                                  attempts, scheduled_for, created_at)
                VALUES (?, ?, ?, 'PENDING', ?, 0, ?, ?)
                """,
                (
                    str(task_type).upper(),
                    doc_id.upper() if doc_id else None,
                    _json_dump(dict(payload or {})),
                    int(priority),
                    scheduled_for,
                    now_iso(),
                ),
            )
            self._commit()
            return int(cursor.lastrowid)

    def claim_next_task(
        self, *, exclude_types: Iterable[str] = ()
    ) -> dict[str, Any] | None:
        excluded = [str(item).upper() for item in exclude_types if item]
        clause = "status='PENDING'"
        params: list[Any] = []
        if excluded:
            clause += f" AND task_type NOT IN ({','.join('?' for _ in excluded)})"
            params.extend(excluded)
        clause += " AND (scheduled_for IS NULL OR scheduled_for <= ?)"
        params.append(now_iso())
        with self._lock:
            row = self.conn.execute(
                f"SELECT * FROM tasks WHERE {clause} "
                "ORDER BY priority DESC, task_id LIMIT 1",
                params,
            ).fetchone()
            if not row:
                return None
            self.conn.execute(
                "UPDATE tasks SET status='RUNNING', attempts=attempts+1, "
                "started_at=? WHERE task_id=?",
                (now_iso(), row["task_id"]),
            )
            self._commit()
            result = dict(row)
            result["status"] = "RUNNING"
            result["attempts"] = int(result["attempts"]) + 1
            return result

    def finish_task(self, task_id: int, *, success: bool, error: str | None = None) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE tasks SET status=?, finished_at=?, last_error=? WHERE task_id=?",
                ("DONE" if success else "FAILED", now_iso(), error, int(task_id)),
            )
            self._commit()

    def reset_task(self, task_id: int) -> None:
        """Return a claimed task to PENDING without losing its attempt count."""
        with self._lock:
            self.conn.execute(
                "UPDATE tasks SET status='PENDING', started_at=NULL WHERE task_id=?",
                (int(task_id),),
            )
            self._commit()

    def list_tasks(self, status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        sql = "SELECT * FROM tasks"
        params: list[Any] = []
        if status:
            sql += " WHERE status = ?"
            params.append(status.upper())
        sql += " ORDER BY priority DESC, task_id LIMIT ?"
        params.append(max(1, min(limit, 2000)))
        with self._lock:
            return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    def documents_updated_between(
        self, since: str, until: str, limit: int = 500
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM documents WHERE updated_at >= ? AND updated_at <= ? "
                "ORDER BY updated_at DESC LIMIT ?",
                (since, until, max(1, min(limit, 2000))),
            ).fetchall()
            return [self._document_dict(row) for row in rows]

    def cards_updated_between(
        self, since: str, until: str, limit: int = 500
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM knowledge_cards "
                "WHERE updated_at >= ? AND updated_at <= ? "
                "ORDER BY updated_at DESC LIMIT ?",
                (since, until, max(1, min(limit, 2000))),
            ).fetchall()
            return [self._card_dict(row) for row in rows]

    def count_tasks(self) -> dict[str, int]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status"
            ).fetchall()
            result = {row["status"]: int(row["n"]) for row in rows}
            result["TOTAL"] = sum(result.values())
            return result

    # ----------------------------------------------------------------- events
    def log_event(
        self,
        level: str,
        category: str,
        message: str,
        *,
        doc_id: str | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO events(ts, level, category, doc_id, message, detail_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    now_iso(),
                    level.upper(),
                    category,
                    doc_id,
                    message,
                    _json_dump(dict(detail or {})),
                ),
            )
            self._commit()

    def recent_events(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM events ORDER BY event_id DESC LIMIT ?",
                (max(1, min(limit, 1000)),),
            ).fetchall()
            return [dict(row) for row in rows]

    # ------------------------------------------------------------------ stats
    def add_token_usage(
        self, doc_id: str | None, input_tokens: int, output_tokens: int
    ) -> None:
        """Accumulate API token usage on a document and in global meta."""
        prompt = max(0, int(input_tokens or 0))
        completion = max(0, int(output_tokens or 0))
        if prompt == 0 and completion == 0:
            return
        with self._lock:
            if doc_id:
                self.conn.execute(
                    "UPDATE documents SET input_tokens=input_tokens+?, "
                    "output_tokens=output_tokens+?, updated_at=? WHERE doc_id=?",
                    (prompt, completion, now_iso(), _clean(doc_id).upper()),
                )
            for key, amount in (
                ("total_input_tokens", prompt),
                ("total_output_tokens", completion),
            ):
                row = self.conn.execute(
                    "SELECT value FROM system_meta WHERE key=?", (key,)
                ).fetchone()
                current = int(row["value"]) if row and str(row["value"]).isdigit() else 0
                self.conn.execute(
                    "INSERT INTO system_meta(key, value, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                    "updated_at=excluded.updated_at",
                    (key, str(current + amount), now_iso()),
                )
            self._commit()

    def token_totals(self) -> dict[str, int]:
        with self._lock:
            def value(key: str) -> int:
                row = self.conn.execute(
                    "SELECT value FROM system_meta WHERE key=?", (key,)
                ).fetchone()
                try:
                    return int(row["value"]) if row else 0
                except (TypeError, ValueError):
                    return 0

            total_in = value("total_input_tokens")
            total_out = value("total_output_tokens")
            return {
                "input_tokens": total_in,
                "output_tokens": total_out,
                "total_tokens": total_in + total_out,
            }

    def stats(self) -> dict[str, Any]:
        with self._lock:
            def scalar(sql: str, params: tuple[Any, ...] = ()) -> int:
                return int(self.conn.execute(sql, params).fetchone()[0])

            download_status = {
                row["download_status"]: int(row["n"])
                for row in self.conn.execute(
                    "SELECT download_status, COUNT(*) AS n FROM documents GROUP BY download_status"
                ).fetchall()
            }
            parse_status = {
                row["parse_status"]: int(row["n"])
                for row in self.conn.execute(
                    "SELECT parse_status, COUNT(*) AS n FROM documents GROUP BY parse_status"
                ).fetchall()
            }
            return {
                "documents": scalar("SELECT COUNT(*) FROM documents"),
                "cards": scalar("SELECT COUNT(*) FROM knowledge_cards"),
                "l1_cards": scalar(
                    "SELECT COUNT(*) FROM knowledge_cards WHERE card_level='L1'"
                ),
                "l2_cards": scalar(
                    "SELECT COUNT(*) FROM knowledge_cards WHERE card_level='L2'"
                ),
                "conflicts": scalar(
                    "SELECT COUNT(*) FROM knowledge_cards WHERE has_conflict=1"
                ),
                "evidence": scalar("SELECT COUNT(*) FROM raw_evidence_chunks"),
                "keywords": scalar("SELECT COUNT(*) FROM controlled_keywords"),
                "download_status": download_status,
                "parse_status": parse_status,
                "manual_required": download_status.get("MANUAL_REQUIRED", 0),
                "downloaded": download_status.get("DOWNLOADED", 0),
                "llm_extracted": parse_status.get("LLM_EXTRACTED", 0),
                "failed_parse": parse_status.get("FAILED", 0),
                "tasks": self.count_tasks(),
                "pending_tasks": self.count_tasks().get("PENDING", 0),
                **self.token_totals(),
            }

    # ------------------------------------------------------------------- rows
    def _document_dict(self, row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
        result = dict(row)
        result["authors_list"] = _load_json(result.get("authors"), [])
        result["keywords_list"] = _load_json(result.get("keywords_json"), [])
        result["metadata"] = _load_json(result.get("metadata_json"), {})
        result["input_tokens"] = int(result.get("input_tokens") or 0)
        result["output_tokens"] = int(result.get("output_tokens") or 0)
        return result

    def _card_dict(self, row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
        result = dict(row)
        result["structured"] = _load_json(result.get("structured_details_json"), {})
        result["generalized_conjecture"] = (
            result["structured"].get("generalized_conjecture")
            or result.get("generalized_conjecture")
        )
        result["supporting_doc_ids_list"] = _load_json(
            result.get("supporting_doc_ids"), []
        )
        result["conflict_with_knw_ids_list"] = _load_json(
            result.get("conflict_with_knw_ids"), []
        )
        result["is_user_verified"] = int(result.get("is_user_verified") or 0)
        result["audit_delete_suggested"] = int(
            result.get("audit_delete_suggested") or 0
        )
        result["audit_hidden"] = int(result.get("audit_hidden") or 0)
        result["audit_review_count"] = int(result.get("audit_review_count") or 0)
        result["audit_positive_count"] = int(
            result.get("audit_positive_count") or 0
        )
        result["audit_negative_count"] = int(
            result.get("audit_negative_count") or 0
        )
        result["audit_divergence"] = int(result.get("audit_divergence") or 0)
        result["needs_vector_update"] = int(
            result.get("needs_vector_update") or 0
        )
        result["master_stale"] = int(result.get("master_stale") or 0)
        result.setdefault("source_kind", "EXTRACTION")
        result.setdefault("audit_status", "NORMAL")
        return result

    def _keyword_dict(self, row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
        result = dict(row)
        result["synonyms_list"] = _load_json(result.get("synonyms_json"), [])
        return result
