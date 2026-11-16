"""Optional MinerU 4 backend for full-document PDF -> Markdown conversion.

The main application never bundles MinerU or its model weights.  This module
detects a user-installed ``mineru-kit`` executable, runs the stateless parser
with a bounded timeout, caches the output by PDF hash + parser settings, and
returns page-aware Markdown for the existing PDFParser/context bundler.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from parser.html_table import (
    html_table_cells,
    html_table_to_markdown,
    markdown_table_cells,
    table_header_rows,
)

log = logging.getLogger(__name__)

PAGE_MARKER = "<!-- ACDB-PAGE: {page} -->"


class MinerUError(RuntimeError):
    pass


class MinerUNotInstalled(MinerUError):
    pass


class MinerUTimeout(MinerUError):
    pass


class MinerUOutputInvalid(MinerUError):
    pass


@dataclass
class MinerUConfig:
    enabled: bool = False
    command: str = "mineru-kit"
    python: str = ""
    tier: str = "standard"
    ocr_mode: str = "auto"
    device: str = "auto"
    timeout_sec: int = 1800
    fallback_to_pymupdf: bool = True
    keep_artifacts: bool = True
    max_output_mb: int = 2048
    min_markdown_chars: int = 200
    concurrent_jobs: int = 1
    language: str = "en"
    model_source: str = ""

    @classmethod
    def from_config(cls, config: Any) -> "MinerUConfig":
        get = getattr(config, "get", None)
        if not callable(get):
            return cls()
        return cls(
            enabled=bool(get("parser.mineru.enabled", False)),
            command=str(get("parser.mineru.command", "mineru-kit") or "mineru-kit"),
            python=str(get("parser.mineru.python", "") or ""),
            tier=str(get("parser.mineru.tier", "standard") or "standard"),
            ocr_mode=str(get("parser.mineru.ocr_mode", "auto") or "auto"),
            device=str(get("parser.mineru.device", "auto") or "auto"),
            timeout_sec=int(get("parser.mineru.timeout_sec", 1800) or 1800),
            fallback_to_pymupdf=bool(
                get("parser.mineru.fallback_to_pymupdf", True)
            ),
            keep_artifacts=bool(get("parser.mineru.keep_artifacts", True)),
            max_output_mb=int(get("parser.mineru.max_output_mb", 2048) or 2048),
            min_markdown_chars=int(
                get("parser.mineru.min_markdown_chars", 200) or 200
            ),
            concurrent_jobs=max(1, int(get("parser.mineru.concurrent_jobs", 1) or 1)),
            language=str(get("parser.mineru.language", "en") or "en"),
            model_source=str(get("parser.mineru.model_source", "") or ""),
        )


@dataclass
class MinerUResult:
    markdown: str
    page_markdown: list[tuple[int, str]]
    page_count: int
    content_list: list[dict[str, Any]] = field(default_factory=list)
    blocks: list[dict[str, Any]] = field(default_factory=list)
    output_dir: Path = Path()
    version: str = ""
    tier: str = ""
    elapsed_sec: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _safe_doc_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value or "document")[:80]


def _safe_extract(archive: zipfile.ZipFile, target: Path) -> None:
    target = target.resolve()
    for info in archive.infolist():
        name = info.filename.replace("\\", "/")
        member = (target / name).resolve()
        if member != target and target not in member.parents:
            raise MinerUOutputInvalid(f"ZIP 包含越界路径：{info.filename}")
        if info.is_dir():
            member.mkdir(parents=True, exist_ok=True)
            continue
        member.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(info) as source, member.open("wb") as output:
            shutil.copyfileobj(source, output)


def _block_markdown(block: Mapping[str, Any]) -> str:
    kind = str(block.get("type") or "").lower()
    content = block.get("content")
    if isinstance(content, list):
        pieces = []
        for item in content:
            if isinstance(item, Mapping):
                pieces.append(_text(item.get("content") or item.get("text")))
            else:
                pieces.append(_text(item))
        content = "\n".join(piece for piece in pieces if piece)
    content = _text(content)
    captions = block.get("captions") or block.get("caption") or []
    if isinstance(captions, str):
        captions = [captions]
    caption_text = "\n".join(_text(item) for item in captions if _text(item))
    if kind in ("header", "footer", "page_header", "page_footer"):
        return ""
    if kind in ("doc_title", "title", "section_title", "heading"):
        level = max(1, min(6, int(block.get("level") or 1)))
        return f"{'#' * level} {content}".strip() if content else ""
    if kind == "table":
        body = content
        if caption_text:
            body = f"{caption_text}\n\n{body}"
        return body
    if kind in ("equation", "formula"):
        if content:
            if content.startswith("$$") or content.startswith("\\["):
                return content
            return f"$$\n{content}\n$$"
        image = _text(block.get("image_source") or block.get("image"))
        return f"![]({image})" if image else ""
    if kind in ("image", "figure", "chart"):
        image = _text(block.get("image_source") or block.get("image") or content)
        if image and not image.startswith("!"):
            return f"![{caption_text}]({image})"
        return content
    if kind == "list":
        return content
    return content


def _block_content_text(block: Mapping[str, Any]) -> str:
    content = block.get("content")
    if isinstance(content, list):
        pieces = []
        for item in content:
            if isinstance(item, Mapping):
                pieces.append(_text(item.get("content") or item.get("text")))
            else:
                pieces.append(_text(item))
        content = "\n".join(piece for piece in pieces if piece)
    return _text(content)


def _blocks_from_structured(
    data: Mapping[str, Any],
    *,
    doc_id: str = "",
) -> list[dict[str, Any]]:
    """Flatten MinerU pages into ordered blocks with page/bbox metadata."""
    pages = data.get("pages") or []
    prefix = _safe_doc_id(doc_id) or "mineru"
    blocks: list[dict[str, Any]] = []
    for index, page in enumerate(pages):
        if not isinstance(page, Mapping):
            continue
        try:
            page_num = int(page.get("page_idx")) + 1
        except (TypeError, ValueError):
            page_num = index + 1
        for block_index, block in enumerate(page.get("blocks") or []):
            if not isinstance(block, Mapping):
                continue
            kind = str(block.get("type") or "").lower()
            text = _block_content_text(block)
            captions = block.get("captions") or block.get("caption") or []
            if isinstance(captions, str):
                captions = [captions]
            caption = "\n".join(_text(item) for item in captions if _text(item))
            image_source = _text(
                block.get("image_source") or block.get("image") or ""
            )
            bbox = block.get("bbox")
            if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
                try:
                    bbox = [float(value) for value in bbox]
                except (TypeError, ValueError):
                    bbox = None
            else:
                bbox = None
            markdown = _block_markdown(block)
            cells: list[list[dict[str, Any]]] = []
            if kind == "table":
                html_markdown = html_table_to_markdown(text)
                if html_markdown:
                    markdown = (f"{caption}\n\n" if caption else "") + html_markdown
                    cells = html_table_cells(text)
                else:
                    cells = markdown_table_cells(text or markdown)
            blocks.append(
                {
                    "block_id": f"{prefix}:p{page_num}:b{block_index:03d}",
                    "page_num": page_num,
                    "block_index": block_index,
                    "type": kind,
                    "text": text,
                    "markdown": markdown,
                    "caption": caption,
                    "image_source": image_source,
                    "bbox": bbox,
                    "cells": cells,
                    "header_rows": table_header_rows(cells),
                }
            )
    return blocks


def _pages_from_structured(data: Mapping[str, Any]) -> list[tuple[int, str]]:
    pages = data.get("pages") or []
    result: list[tuple[int, str]] = []
    for index, page in enumerate(pages):
        if not isinstance(page, Mapping):
            continue
        page_idx = page.get("page_idx")
        try:
            page_num = int(page_idx) + 1
        except (TypeError, ValueError):
            page_num = index + 1
        blocks = page.get("blocks") or []
        pieces: list[str] = []
        for block in blocks:
            if not isinstance(block, Mapping):
                continue
            text = _block_markdown(block)
            if text:
                pieces.append(text)
        if pieces:
            result.append((page_num, "\n\n".join(pieces)))
    return result


def _pages_from_middle(data: Mapping[str, Any]) -> list[tuple[int, str]]:
    pages = data.get("pages") or []
    result: list[tuple[int, str]] = []
    for index, page in enumerate(pages):
        if not isinstance(page, Mapping):
            continue
        try:
            page_num = int(page.get("page_idx")) + 1
        except (TypeError, ValueError):
            page_num = index + 1
        pieces = []
        for block in page.get("blocks") or []:
            if not isinstance(block, Mapping):
                continue
            content = block.get("content")
            text = ""
            if isinstance(content, list):
                text = "\n".join(
                    _text(item.get("content") if isinstance(item, Mapping) else item)
                    for item in content
                )
            else:
                text = _text(content)
            if text:
                pieces.append(text)
        if pieces:
            result.append((page_num, "\n\n".join(pieces)))
    return result


def _combine_pages(pages: list[tuple[int, str]]) -> str:
    return "\n\n".join(
        f"{PAGE_MARKER.format(page=page)}\n{text}" for page, text in pages
    )


class MinerUConverter:
    def __init__(
        self,
        workspace: str | Path,
        config: MinerUConfig | None = None,
    ):
        self.workspace = Path(workspace)
        self.config = config or MinerUConfig()
        self._version_cache: str | None = None
        self._semaphore = threading.BoundedSemaphore(
            max(1, int(self.config.concurrent_jobs or 1))
        )
        self.last_error: str = ""
        self.last_command: list[str] = []

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled)

    def _executable(self) -> str:
        command = self.config.command.strip() or "mineru-kit"
        path = Path(command)
        if path.is_file():
            return str(path)
        found = shutil.which(command)
        if not found:
            raise MinerUNotInstalled(
                f"找不到 MinerU 命令：{command}。请安装 MinerU 4 或填写完整路径。"
            )
        return found

    def version(self) -> str:
        if self._version_cache:
            return self._version_cache
        exe = self._executable()
        for args in (["version", "--json"], ["--version"], ["version"]):
            try:
                completed = subprocess.run(
                    [exe, *args],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=20,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            output = (completed.stdout or "") + "\n" + (completed.stderr or "")
            match = re.search(r"(\d+\.\d+\.\d+)", output)
            if match:
                self._version_cache = match.group(1)
                return self._version_cache
        self._version_cache = "unknown"
        return self._version_cache

    def probe(self) -> dict[str, Any]:
        try:
            exe = self._executable()
            version = self.version()
            return {
                "available": True,
                "command": exe,
                "version": version,
                "error": "",
            }
        except Exception as exc:
            return {
                "available": False,
                "command": self.config.command,
                "version": "",
                "error": str(exc),
            }

    def _cache_dir(self, doc_id: str, pdf_sha256: str) -> Path:
        return (
            self.workspace
            / "mineru"
            / _safe_doc_id(doc_id)
            / pdf_sha256[:20]
        )

    def _cache_key(self, pdf_sha256: str, version: str) -> str:
        raw = "|".join(
            (
                pdf_sha256,
                version,
                self.config.tier,
                self.config.ocr_mode,
                self.config.language,
                self.config.command,
            )
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    def _load_cached(self, cache_dir: Path, manifest_path: Path) -> MinerUResult | None:
        if not manifest_path.is_file():
            return None
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if manifest.get("status") != "ok":
            return None
        pages_path = cache_dir / "page_markdown.json"
        markdown_path = cache_dir / "full.md"
        if not pages_path.is_file() or not markdown_path.is_file():
            return None
        try:
            pages = [
                (int(page), str(text))
                for page, text in json.loads(
                    pages_path.read_text(encoding="utf-8")
                )
            ]
        except (OSError, ValueError, TypeError):
            return None
        blocks: list[dict[str, Any]] = []
        blocks_path = cache_dir / "blocks.json"
        if blocks_path.is_file():
            try:
                raw_blocks = json.loads(blocks_path.read_text(encoding="utf-8"))
                if isinstance(raw_blocks, list):
                    blocks = [dict(item) for item in raw_blocks if isinstance(item, Mapping)]
            except (OSError, ValueError, TypeError):
                blocks = []
        return MinerUResult(
            markdown=markdown_path.read_text(encoding="utf-8"),
            page_markdown=pages,
            page_count=int(manifest.get("page_count") or len(pages)),
            blocks=blocks,
            output_dir=cache_dir,
            version=str(manifest.get("version") or ""),
            tier=str(manifest.get("tier") or ""),
            elapsed_sec=0.0,
            metadata=dict(manifest.get("metadata") or {}),
        )

    def convert(
        self,
        pdf_path: str | Path,
        *,
        doc_id: str,
        pdf_sha256: str = "",
    ) -> MinerUResult:
        pdf = Path(pdf_path)
        if not pdf.is_file():
            raise FileNotFoundError(pdf)
        if not pdf_sha256:
            pdf_sha256 = hashlib.sha256(pdf.read_bytes()).hexdigest()
        version = self.version()
        cache_dir = self._cache_dir(doc_id, pdf_sha256)
        cache_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = cache_dir / "manifest.json"
        cached = self._load_cached(cache_dir, manifest_path)
        if cached is not None:
            return cached

        exe = self._executable()
        result_zip = cache_dir / "result.zip"
        extracted = cache_dir / "extracted"
        if result_zip.exists():
            result_zip.unlink()
        if extracted.exists():
            shutil.rmtree(extracted, ignore_errors=True)
        command = [
            exe,
            "parse",
            str(pdf),
            "-o",
            str(result_zip),
            "--format",
            "zip",
            "--tier",
            self.config.tier,
            "--ocr-mode",
            self.config.ocr_mode,
            "--pages",
            "all",
        ]
        self.last_command = command
        env = os.environ.copy()
        if self.config.device and self.config.device != "auto":
            env["MINERU_MODEL_ONNX_DEVICE"] = self.config.device
        if self.config.model_source:
            env["MINERU_MODEL_SOURCE"] = self.config.model_source
        started = time.perf_counter()
        try:
            with self._semaphore:
                completed = subprocess.run(
                    command,
                    cwd=str(cache_dir),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=max(30, int(self.config.timeout_sec)),
                    env=env,
                )
        except subprocess.TimeoutExpired as exc:
            self.last_error = f"MinerU 超时（{self.config.timeout_sec}s）"
            raise MinerUTimeout(self.last_error) from exc
        except OSError as exc:
            self.last_error = f"MinerU 启动失败：{exc}"
            raise MinerUNotInstalled(self.last_error) from exc
        elapsed = time.perf_counter() - started
        if completed.returncode != 0:
            tail = ((completed.stderr or "") + "\n" + (completed.stdout or ""))[-4000:]
            self.last_error = f"MinerU 退出码 {completed.returncode}：{tail}"
            self._write_manifest(
                manifest_path,
                status="failed",
                version=version,
                pdf_sha256=pdf_sha256,
                elapsed=elapsed,
                error=self.last_error,
            )
            raise MinerUError(self.last_error)
        if not result_zip.is_file() or result_zip.stat().st_size == 0:
            self.last_error = "MinerU 没有生成 ZIP 输出"
            raise MinerUOutputInvalid(self.last_error)
        max_bytes = max(1, int(self.config.max_output_mb)) * 1024 * 1024
        if result_zip.stat().st_size > max_bytes:
            self.last_error = f"MinerU ZIP 超过 {self.config.max_output_mb} MB"
            raise MinerUOutputInvalid(self.last_error)
        extracted.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(result_zip) as archive:
                _safe_extract(archive, extracted)
        except (OSError, zipfile.BadZipFile) as exc:
            self.last_error = f"MinerU ZIP 解包失败：{exc}"
            raise MinerUOutputInvalid(self.last_error) from exc

        structured_path = _find_file(extracted, "structured_content.json")
        middle_path = _find_file(extracted, "middle_json.json")
        markdown_path = _find_file(extracted, "markdown.md") or _find_file(
            extracted, "full.md"
        )
        pages: list[tuple[int, str]] = []
        blocks: list[dict[str, Any]] = []
        metadata: dict[str, Any] = {}
        if structured_path:
            try:
                structured = json.loads(structured_path.read_text(encoding="utf-8"))
                pages = _pages_from_structured(structured)
                blocks = _blocks_from_structured(structured, doc_id=doc_id)
                metadata = dict(structured.get("metadata") or {})
            except (OSError, ValueError):
                pages = []
                blocks = []
        if not pages and middle_path:
            try:
                middle = json.loads(middle_path.read_text(encoding="utf-8"))
                pages = _pages_from_middle(middle)
                metadata = dict(middle.get("metadata") or {})
            except (OSError, ValueError):
                pages = []
        if not pages and markdown_path:
            text = markdown_path.read_text(encoding="utf-8")
            if text.strip():
                pages = [(1, text)]
        if not pages:
            self.last_error = "MinerU 输出没有可用 Markdown/结构化页面"
            raise MinerUOutputInvalid(self.last_error)
        markdown = _combine_pages(pages)
        if len(re.sub(r"\s+", "", markdown)) < max(1, self.config.min_markdown_chars):
            self.last_error = "MinerU 输出文本过少"
            raise MinerUOutputInvalid(self.last_error)
        page_count = int(
            metadata.get("document", {}).get("page_count")
            or len(pages)
            or 0
        )
        full_path = cache_dir / "full.md"
        full_path.write_text(markdown, encoding="utf-8")
        pages_path = cache_dir / "page_markdown.json"
        pages_path.write_text(
            json.dumps(pages, ensure_ascii=False), encoding="utf-8"
        )
        (cache_dir / "blocks.json").write_text(
            json.dumps(blocks, ensure_ascii=False), encoding="utf-8"
        )
        self._write_manifest(
            manifest_path,
            status="ok",
            version=version,
            pdf_sha256=pdf_sha256,
            elapsed=elapsed,
            error="",
            page_count=page_count,
            metadata=metadata,
        )
        if not self.config.keep_artifacts:
            shutil.rmtree(extracted, ignore_errors=True)
        return MinerUResult(
            markdown=markdown,
            page_markdown=pages,
            page_count=page_count,
            blocks=blocks,
            output_dir=cache_dir,
            version=version,
            tier=self.config.tier,
            elapsed_sec=elapsed,
            metadata=metadata,
        )

    def _write_manifest(
        self,
        path: Path,
        *,
        status: str,
        version: str,
        pdf_sha256: str,
        elapsed: float,
        error: str,
        page_count: int = 0,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        payload = {
            "status": status,
            "version": version,
            "tier": self.config.tier,
            "ocr_mode": self.config.ocr_mode,
            "device": self.config.device,
            "pdf_sha256": pdf_sha256,
            "elapsed_sec": round(elapsed, 3),
            "page_count": page_count,
            "command": self.last_command,
            "error": error[-4000:],
            "metadata": dict(metadata or {}),
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def clear_cache(self, doc_id: str | None = None) -> int:
        root = self.workspace / "mineru"
        target = root / _safe_doc_id(doc_id) if doc_id else root
        if not target.exists():
            return 0
        count = sum(1 for path in target.rglob("*") if path.is_file())
        shutil.rmtree(target, ignore_errors=True)
        return count


def _find_file(root: Path, name: str) -> Path | None:
    for path in root.rglob(name):
        if path.is_file():
            return path
    return None
