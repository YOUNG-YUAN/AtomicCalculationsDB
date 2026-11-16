"""Incremental WebDAV / S3 workspace sync.

A sync manifest is built from a consistent SQLite backup plus new PDFs and
Qdrant files.  Only entries whose SHA-256 differs from ``sync_state`` are
uploaded.  Pulling a remote database snapshot is deliberately staged under
``workspace/sync_incoming/`` rather than replacing a live SQLite file.

Cloud SDKs are optional: if ``webdav4`` / ``boto3`` is missing, the provider is
reported as unavailable instead of crashing the app.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from db.sqlite_manager import SQLiteManager

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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass
class SyncResult:
    provider: str
    ok: bool
    uploaded: int = 0
    downloaded: int = 0
    skipped: int = 0
    merged_documents: int = 0
    merged_cards: int = 0
    updated_cards: int = 0
    message: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "ok": self.ok,
            "uploaded": self.uploaded,
            "downloaded": self.downloaded,
            "skipped": self.skipped,
            "merged_documents": self.merged_documents,
            "merged_cards": self.merged_cards,
            "updated_cards": self.updated_cards,
            "message": self.message,
        }


@dataclass
class SyncEntry:
    path: str  # workspace-relative
    sha256: str
    size: int

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "sha256": self.sha256, "size": self.size}


class CloudSyncManager:
    def __init__(
        self,
        db: SQLiteManager,
        workspace_dir: str | Path,
        *,
        config: Any = None,
        vector_dir: str | Path | None = None,
        webdav_backend: Any = None,
        s3_backend: Any = None,
        packager: Any = None,
        vectorizer: Any = None,
    ):
        self.db = db
        self.workspace = Path(workspace_dir)
        self.config = config
        self.vector_dir = Path(vector_dir or (self.workspace / "vector_db"))
        self.snapshots = self.workspace / "sync_snapshots"
        self.incoming = self.workspace / "sync_incoming"
        self.webdav_backend = webdav_backend
        self.s3_backend = s3_backend
        self.packager = packager
        self.vectorizer = vectorizer

    # -------------------------------------------------------------- manifest
    def build_manifest(self) -> dict[str, Any]:
        self.snapshots.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        snapshot = self.snapshots / f"atomic_meta_{stamp}.sqlite"
        self.db.backup_to(snapshot)
        entries: list[SyncEntry] = [
            SyncEntry(
                path=str(snapshot.relative_to(self.workspace)),
                sha256=_sha256(snapshot),
                size=snapshot.stat().st_size,
            )
        ]
        for base in (self.vector_dir, self.workspace / "storage"):
            if not base.exists():
                continue
            for path in sorted(base.rglob("*")):
                if not path.is_file():
                    continue
                entries.append(
                    SyncEntry(
                        path=str(path.relative_to(self.workspace)),
                        sha256=_sha256(path),
                        size=path.stat().st_size,
                    )
                )
        manifest = {
            "format": "acdb-sync/1",
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "embedding_signature": self.db.embedding_signature(),
            "entries": [entry.as_dict() for entry in entries],
        }
        (self.snapshots / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return manifest

    def _local_path(self, relative: str) -> Path:
        path = (self.workspace / relative).resolve()
        if self.workspace.resolve() not in path.parents and path != self.workspace.resolve():
            raise ValueError(f"非法同步路径: {relative}")
        return path

    def _changed(self, manifest: Mapping[str, Any]) -> list[SyncEntry]:
        changed: list[SyncEntry] = []
        for item in manifest.get("entries") or []:
            relative = str(item.get("path") or "")
            if not relative:
                continue
            row = self.db.conn.execute(
                "SELECT sha256 FROM sync_state WHERE path = ?", (relative,)
            ).fetchone()
            if row is None or row["sha256"] != item.get("sha256"):
                changed.append(
                    SyncEntry(relative, str(item.get("sha256")), int(item.get("size") or 0))
                )
        return changed

    def _record(self, entry: SyncEntry, remote: str) -> None:
        with self.db._lock:
            self.db.conn.execute(
                "INSERT INTO sync_state(path, sha256, size, mtime, synced_at, remote) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(path) DO UPDATE SET sha256=excluded.sha256, "
                "size=excluded.size, synced_at=excluded.synced_at, remote=excluded.remote",
                (
                    entry.path,
                    entry.sha256,
                    entry.size,
                    self._local_path(entry.path).stat().st_mtime,
                    datetime.now().astimezone().isoformat(timespec="seconds"),
                    remote,
                ),
            )
            self.db.conn.commit()

    # --------------------------------------------------------------- WebDAV
    def sync_webdav(self) -> SyncResult:
        if not bool(_cfg(self.config, "sync.webdav.enabled", False)):
            return SyncResult("webdav", False, message="WebDAV 未启用")
        try:
            backend = self.webdav_backend or self._webdav_backend()
        except Exception as exc:
            return SyncResult("webdav", False, message=str(exc))
        return self._push_pull("webdav", backend, str(_cfg(self.config, "sync.webdav.remote_dir", "/AtomicCalculationDB")))

    def _webdav_backend(self) -> Any:
        cfg = self.config
        try:
            from webdav4.client import Client
        except ImportError as exc:
            raise RuntimeError("webdav4 未安装") from exc
        url = str(_cfg(cfg, "sync.webdav.base_url", "") or "")
        username = str(_cfg(cfg, "sync.webdav.username", "") or "")
        password = str(_cfg(cfg, "sync.webdav.password", "") or "")
        if not url:
            raise RuntimeError("WebDAV base_url 未配置")
        client = Client(url, auth=(username, password))
        return _WebDAVBackend(client)

    # ------------------------------------------------------------------- S3
    def sync_s3(self) -> SyncResult:
        if not bool(_cfg(self.config, "sync.s3.enabled", False)):
            return SyncResult("s3", False, message="S3 未启用")
        try:
            backend = self.s3_backend or self._s3_backend()
        except Exception as exc:
            return SyncResult("s3", False, message=str(exc))
        prefix = str(_cfg(self.config, "sync.s3.prefix", "AtomicCalculationDB") or "")
        return self._push_pull("s3", backend, prefix)

    def _s3_backend(self) -> Any:
        cfg = self.config
        try:
            import boto3
        except ImportError as exc:
            raise RuntimeError("boto3 未安装") from exc
        bucket = str(_cfg(cfg, "sync.s3.bucket", "") or "")
        if not bucket:
            raise RuntimeError("S3 bucket 未配置")
        client = boto3.client(
            "s3",
            endpoint_url=str(_cfg(cfg, "sync.s3.endpoint_url", "") or None),
            aws_access_key_id=str(_cfg(cfg, "sync.s3.access_key", "") or None),
            aws_secret_access_key=str(_cfg(cfg, "sync.s3.secret_key", "") or None),
        )
        return _S3Backend(client, bucket)

    # ------------------------------------------------------------ push/pull
    def _push_pull(self, provider: str, backend: Any, remote_root: str) -> SyncResult:
        result = SyncResult(provider, True)
        try:
            manifest = self.build_manifest()
            remote_manifest = backend.read_manifest(remote_root)
            changed = self._changed(manifest)
            for entry in changed:
                local = self._local_path(entry.path)
                if not local.is_file():
                    result.skipped += 1
                    continue
                backend.upload(local, f"{remote_root.rstrip('/')}/{entry.path}")
                self._record(entry, f"{remote_root.rstrip('/')}/{entry.path}")
                result.uploaded += 1
            if remote_manifest:
                local_paths = {item["path"] for item in manifest.get("entries") or []}
                database_entries = [
                    item
                    for item in remote_manifest.get("entries") or []
                    if item.get("path", "").endswith(".sqlite")
                ]
                for item in database_entries[:1]:
                    relative = str(item.get("path") or "")
                    remote_key = f"remote:{provider}:{relative}"
                    with self.db._lock:
                        previous = self.db.conn.execute(
                            "SELECT sha256 FROM sync_state WHERE path=?", (remote_key,)
                        ).fetchone()
                    if (
                        not relative
                        or relative in local_paths
                        or (previous and previous["sha256"] == item.get("sha256"))
                    ):
                        continue
                    self.incoming.mkdir(parents=True, exist_ok=True)
                    target = self.incoming / Path(relative).name
                    backend.download(f"{remote_root.rstrip('/')}/{relative}", target)
                    result.downloaded += 1
                    counts = self._merge_downloaded_snapshot(target)
                    result.merged_documents += int(counts.get("documents_added") or 0)
                    result.merged_cards += int(counts.get("cards_added") or 0)
                    result.updated_cards += int(counts.get("cards_updated") or 0)
                    with self.db._lock:
                        self.db.conn.execute(
                            "INSERT INTO sync_state(path, sha256, size, mtime, synced_at, remote) "
                            "VALUES (?, ?, ?, ?, ?, ?) "
                            "ON CONFLICT(path) DO UPDATE SET sha256=excluded.sha256, "
                            "synced_at=excluded.synced_at, remote=excluded.remote",
                            (
                                remote_key,
                                str(item.get("sha256") or ""),
                                int(item.get("size") or 0),
                                target.stat().st_mtime if target.exists() else 0,
                                datetime.now().astimezone().isoformat(timespec="seconds"),
                                f"{remote_root.rstrip('/')}/{relative}",
                            ),
                        )
                        self.db.conn.commit()
                    target.unlink(missing_ok=True)
            manifest["provider"] = provider
            backend.write_manifest(remote_root, manifest)
            if result.merged_documents or result.merged_cards or result.updated_cards:
                result.message = (
                    f"云端同步完成：新增 {result.merged_documents} 篇文献、"
                    f"{result.merged_cards} 张知识卡，更新 {result.updated_cards} 张卡片"
                    f"（上传 {result.uploaded} 个文件）"
                )
            else:
                result.message = (
                    f"上传 {result.uploaded} 个文件，下载 {result.downloaded} 个数据库快照"
                )
        except Exception as exc:
            log.exception("%s sync failed", provider)
            result.ok = False
            result.message = str(exc)
        return result

    def _merge_downloaded_snapshot(self, target: Path) -> dict[str, int]:
        if self.packager is None:
            log.warning("No packager configured; snapshot left at %s", target)
            return {}
        counts = self.packager.merge_snapshot(
            target, vectorizer=self.vectorizer
        )
        target.unlink(missing_ok=True)
        # Remove any stale temp snapshots left by earlier interrupted pulls.
        for stale in self.incoming.glob("*.sqlite*"):
            try:
                stale.unlink()
            except OSError:
                log.debug("Could not remove stale sync temp %s", stale)
        return counts

    def status(self) -> dict[str, Any]:
        return {
            "webdav": bool(_cfg(self.config, "sync.webdav.enabled", False)),
            "s3": bool(_cfg(self.config, "sync.s3.enabled", False)),
            "snapshot_dir": str(self.snapshots),
            "incoming_dir": str(self.incoming),
            "auto_merge": self.packager is not None,
        }


class _WebDAVBackend:
    def __init__(self, client: Any):
        self.client = client

    def upload(self, local: Path, remote: str) -> None:
        parent = str(Path(remote).parent).replace("\\", "/")
        try:
            if not self.client.exists(parent):
                self.client.mkdir(parent, parents=True)
        except Exception:
            pass
        self.client.upload_file(str(local), remote)

    def download(self, remote: str, local: Path) -> None:
        local.parent.mkdir(parents=True, exist_ok=True)
        self.client.download_file(remote, str(local))

    def read_manifest(self, remote_root: str) -> dict[str, Any] | None:
        path = f"{remote_root.rstrip('/')}/manifest.json"
        try:
            if not self.client.exists(path):
                return None
            text = self.client.read_text(path)
            return json.loads(text)
        except Exception:
            return None

    def write_manifest(self, remote_root: str, manifest: Mapping[str, Any]) -> None:
        import tempfile

        path = f"{remote_root.rstrip('/')}/manifest.json"
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        ) as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2)
            temp = handle.name
        try:
            self.client.upload_file(temp, path)
        finally:
            Path(temp).unlink(missing_ok=True)


class _S3Backend:
    def __init__(self, client: Any, bucket: str):
        self.client = client
        self.bucket = bucket

    def upload(self, local: Path, remote: str) -> None:
        self.client.upload_file(str(local), self.bucket, remote)

    def download(self, remote: str, local: Path) -> None:
        local.parent.mkdir(parents=True, exist_ok=True)
        self.client.download_file(self.bucket, remote, str(local))

    def read_manifest(self, remote_root: str) -> dict[str, Any] | None:
        key = f"{remote_root.rstrip('/')}/manifest.json"
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            return json.loads(response["Body"].read().decode("utf-8"))
        except Exception:
            return None

    def write_manifest(self, remote_root: str, manifest: Mapping[str, Any]) -> None:
        key = f"{remote_root.rstrip('/')}/manifest.json"
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
