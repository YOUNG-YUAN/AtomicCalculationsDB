"""Four-level waterfall downloader.

Level 1  arXiv source (LaTeX preferred) / PDF
Level 2  publisher DOI page via Playwright with the local/campus IP session
Level 3  explicitly configured fallback mirrors (disabled by default)
Level 4  manual-download queue

Level 3 is intentionally opt-in (``crawler.scihub_enabled: true``) and has no
hard-coded domains.  Users are responsible for using only sources they are
authorised to access.  The default configuration leaves it disabled.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import quote

from crawler.discovery_api import DiscoveryService, DocumentCandidate
from crawler.title_gatekeeper import TitleGatekeeper
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


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass
class FetchResult:
    doc_id: str
    status: str
    source: str = ""
    pdf_path: str = ""
    source_archive: str = ""
    message: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "status": self.status,
            "source": self.source,
            "pdf_path": self.pdf_path,
            "source_archive": self.source_archive,
            "message": self.message,
        }


class WaterfallFetcher:
    def __init__(
        self,
        db: SQLiteManager,
        workspace_dir: str | Path,
        *,
        config: Any = None,
        downloader: Callable[[str, Path], bool] | None = None,
        playwright_fetcher: Callable[[str, Path], Path | None] | None = None,
        discovery: DiscoveryService | None = None,
        gatekeeper: TitleGatekeeper | None = None,
    ):
        self.db = db
        self.workspace = Path(workspace_dir)
        self.config = config
        self.downloader = downloader
        self.playwright_fetcher = playwright_fetcher
        self.discovery = discovery
        self.gatekeeper = gatekeeper or TitleGatekeeper()
        self.storage_root = self.workspace / "storage"
        self.fallback_mirrors = list(
            _cfg(config, "crawler.scihub_mirrors", []) or []
        )
        self.fallback_enabled = bool(
            _cfg(config, "crawler.scihub_enabled", False)
        )
        self.fallback_max_year = int(
            _cfg(config, "crawler.fallback_max_year", 2021) or 2021
        )
        self.playwright_enabled = bool(
            _cfg(config, "crawler.playwright_enabled", True)
        )
        self.timeout = float(_cfg(config, "crawler.request_timeout_sec", 60) or 60)

    # --------------------------------------------------------------- public
    def fetch(self, doc_id: str) -> FetchResult:
        doc = self.db.get_document(doc_id)
        if not doc:
            return FetchResult(doc_id, "FAILED", message="文献不存在")
        local_pdf = self._absolute(doc.get("local_pdf_path"))
        if local_pdf and local_pdf.exists():
            return FetchResult(
                doc_id, "ALREADY_PRESENT", pdf_path=str(local_pdf)
            )

        gate = self.gatekeeper.evaluate(
            doc.get("title", ""),
            abstract=doc.get("abstract", ""),
            keywords=doc.get("keywords_list") or [],
            journal=doc.get("journal", ""),
            doc_type=doc.get("doc_type", "PAP"),
        )
        if gate.rejected:
            self.db.update_download_status(
                doc_id, "REJECTED", error="; ".join(gate.reasons)
            )
            return FetchResult(
                doc_id, "REJECTED", message="; ".join(gate.reasons)
            )

        for level, handler in (
            ("arxiv", self._try_arxiv),
            ("publisher", self._try_publisher),
            ("fallback", self._try_fallback),
        ):
            try:
                result = handler(doc)
            except Exception as exc:
                log.exception("Level %s failed for %s: %s", level, doc_id, exc)
                result = None
            if result:
                return result

        self.db.update_download_status(
            doc_id, "MANUAL_REQUIRED", error="文献自动下载未取得 PDF"
        )
        self.db.log_event(
            "WARN",
            "fetch",
            f"{doc_id} 转入待手动下载清单",
            doc_id=doc_id,
        )
        return FetchResult(
            doc_id,
            "MANUAL_REQUIRED",
            message="文献自动下载未取得 PDF，已转入待手动下载清单",
        )

    def fetch_pending(self, limit: int = 20) -> list[FetchResult]:
        documents = [
            doc
            for status in ("DISCOVERED", "QUEUED")
            for doc in self.db.list_documents(download_status=status, limit=limit)
        ]
        results: list[FetchResult] = []
        seen: set[str] = set()
        for doc in documents:
            if doc["doc_id"] in seen:
                continue
            seen.add(doc["doc_id"])
            results.append(self.fetch(doc["doc_id"]))
            if len(results) >= limit:
                break
        return results

    def discover_and_queue(
        self,
        query: str,
        *,
        sources: Iterable[str] = ("openalex", "crossref", "arxiv"),
        limit_per_source: int = 20,
    ) -> list[str]:
        discovery = self.discovery or DiscoveryService()
        candidates = discovery.discover(
            query, sources=sources, limit_per_source=limit_per_source
        )
        doc_ids: list[str] = []
        for candidate in candidates:
            doc = candidate.to_document()
            doc_id = self.db.upsert_document(doc)
            if candidate.rejected:
                self.db.update_download_status(
                    doc_id, "REJECTED", error="; ".join(candidate.gatekeeper_reasons)
                )
            else:
                self.db.add_task("FETCH", doc_id=doc_id, priority=5)
                doc_ids.append(doc_id)
        self.db.log_event(
            "INFO",
            "discovery",
            f"寻源 '{query}'：{len(candidates)} 条候选，{len(doc_ids)} 条进入下载队列",
        )
        return doc_ids

    # -------------------------------------------------------------- level 1
    def _try_arxiv(self, doc: Mapping[str, Any]) -> FetchResult | None:
        arxiv_id = str(doc.get("arxiv_id") or "").strip()
        if not arxiv_id:
            return None
        directory = self._storage_dir(doc["doc_id"])
        pdf_target = directory / "arxiv.pdf"
        source_target = directory / "arxiv-source.tar.gz"
        pdf_ok = self._download(
            f"https://arxiv.org/pdf/{quote(arxiv_id, safe='/')}.pdf",
            pdf_target,
            expect_pdf=True,
        )
        source_ok = self._download(
            f"https://arxiv.org/e-print/{quote(arxiv_id, safe='/')}",
            source_target,
            expect_pdf=False,
        )
        if pdf_ok:
            self._attach(doc["doc_id"], pdf_target, "arxiv")
            if source_ok:
                self.db.upsert_document(
                    {
                        "doc_id": doc["doc_id"],
                        "metadata_json": {
                            "arxiv_source": str(
                                source_target.relative_to(self.workspace)
                            )
                        },
                    }
                )
            return FetchResult(
                doc["doc_id"],
                "DOWNLOADED",
                source="arxiv",
                pdf_path=str(pdf_target),
                source_archive=str(source_target) if source_ok else "",
            )
        if source_ok:
            # LaTeX source is still highly valuable; mark downloaded and let
            # the parser decide how to handle it.
            self.db.update_download_status(doc["doc_id"], "DOWNLOADED", source_url=doc.get("source_url"))
            self.db.add_task(
                "PARSE",
                doc_id=doc["doc_id"],
                payload={"source_archive": str(source_target.relative_to(self.workspace))},
                priority=10,
            )
            return FetchResult(
                doc["doc_id"],
                "DOWNLOADED",
                source="arxiv-source",
                source_archive=str(source_target),
                message="仅取得 arXiv LaTeX 源码包",
            )
        return None

    # -------------------------------------------------------------- level 2
    def _try_publisher(self, doc: Mapping[str, Any]) -> FetchResult | None:
        doi = str(doc.get("doi") or "").strip()
        if not doi:
            return None
        directory = self._storage_dir(doc["doc_id"])
        target = directory / "publisher.pdf"
        fetcher = self.playwright_fetcher or self._playwright_download
        try:
            path = fetcher(doi, directory)
        except Exception as exc:
            log.info("Playwright publisher fetch failed for %s: %s", doi, exc)
            return None
        if path and Path(path).exists():
            target_path = Path(path)
            if target_path.suffix.lower() != ".pdf":
                target_path = target
            if target_path.exists():
                self._attach(doc["doc_id"], target_path, "publisher", source_url=f"https://doi.org/{doi}")
                return FetchResult(
                    doc["doc_id"],
                    "DOWNLOADED",
                    source="publisher",
                    pdf_path=str(target_path),
                )
        return None

    def _playwright_download(self, doi: str, directory: Path) -> Path | None:
        """Persistent Chromium context reusing campus-network/IP cookies."""
        if not self.playwright_enabled:
            log.info("Playwright downloader disabled by config")
            return None
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            log.info("Playwright not installed: %s", exc)
            return None
        target = directory / "publisher.pdf"
        profile = self.workspace / "browser_profile"
        profile.mkdir(parents=True, exist_ok=True)
        headless = bool(_cfg(self.config, "crawler.playwright_headless", True))
        try:
            with sync_playwright() as playwright:
                context = playwright.chromium.launch_persistent_context(
                    user_data_dir=str(profile),
                    headless=headless,
                    accept_downloads=True,
                    viewport={"width": 1366, "height": 900},
                )
                try:
                    page = context.pages[0] if context.pages else context.new_page()
                    page.goto(
                        f"https://doi.org/{doi}",
                        wait_until="domcontentloaded",
                        timeout=int(self.timeout * 1000),
                    )
                    page.wait_for_timeout(2500)
                    candidates = page.locator(
                        'a[href$=".pdf"], a:has-text("Download PDF"), '
                        'a:has-text("PDF"), button:has-text("Download PDF")'
                    )
                    for index in range(min(candidates.count(), 8)):
                        locator = candidates.nth(index)
                        try:
                            with page.expect_download(timeout=20000) as download_info:
                                locator.click()
                            download = download_info.value
                            download.save_as(str(target))
                            if self._looks_like_pdf(target):
                                return target
                        except Exception:
                            continue
                    # Some publishers expose a direct PDF URL in an embed/iframe.
                    pdf_url = page.evaluate(
                        """() => {
                          const nodes = [...document.querySelectorAll('embed,iframe,object,a')];
                          for (const node of nodes) {
                            const src = node.src || node.data || node.href || '';
                            if (src && /pdf/i.test(src)) return src;
                          }
                          return '';
                        }"""
                    )
                    if pdf_url:
                        if not pdf_url.startswith("http"):
                            pdf_url = "https://doi.org" + pdf_url
                        if self._download(pdf_url, target, expect_pdf=True):
                            return target
                finally:
                    context.close()
        except Exception as exc:
            log.info("Playwright session failed: %s", exc)
        return None

    # -------------------------------------------------------------- level 3
    def _try_fallback(self, doc: Mapping[str, Any]) -> FetchResult | None:
        if not self.fallback_enabled or not self.fallback_mirrors:
            return None
        doi = str(doc.get("doi") or "").strip()
        if not doi:
            return None
        year = doc.get("pub_year")
        if year and int(year) > self.fallback_max_year:
            return None
        directory = self._storage_dir(doc["doc_id"])
        target = directory / "fallback.pdf"
        for mirror in self.fallback_mirrors:
            url = f"{str(mirror).rstrip('/')}/{quote(doi, safe='')}"
            try:
                pdf_url = self._extract_pdf_link(url)
                if not pdf_url:
                    continue
                if pdf_url.startswith("//"):
                    pdf_url = "https:" + pdf_url
                elif pdf_url.startswith("/"):
                    pdf_url = str(mirror).rstrip("/") + pdf_url
                if self._download(pdf_url, target, expect_pdf=True):
                    self._attach(doc["doc_id"], target, "fallback")
                    self.db.log_event(
                        "WARN",
                        "fetch",
                        f"{doc['doc_id']} 使用了用户配置的备用下载源",
                        doc_id=doc["doc_id"],
                    )
                    return FetchResult(
                        doc["doc_id"],
                        "DOWNLOADED",
                        source="fallback",
                        pdf_path=str(target),
                    )
            except Exception as exc:
                log.info("Fallback mirror failed: %s", exc)
                continue
        return None

    def _extract_pdf_link(self, page_url: str) -> str:
        import httpx

        with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
            response = client.get(page_url)
            response.raise_for_status()
            html = response.text
        try:
            from bs4 import BeautifulSoup

            soup = BeautifulSoup(html, "lxml")
            for tag, attr in (
                ("embed", "src"),
                ("iframe", "src"),
                ("object", "data"),
                ("a", "href"),
            ):
                for node in soup.find_all(tag):
                    value = node.get(attr) or ""
                    if "pdf" in value.lower():
                        return value
        except Exception:
            match = re.search(r'(?:src|href)=["\']([^"\']*pdf[^"\']*)', html, re.I)
            if match:
                return match.group(1)
        return ""

    # -------------------------------------------------------------- helpers
    def _storage_dir(self, doc_id: str) -> Path:
        directory = self.storage_root / doc_id
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _absolute(self, value: Any) -> Path | None:
        if not value:
            return None
        path = Path(str(value))
        return path if path.is_absolute() else self.workspace / path

    def _attach(self, doc_id: str, pdf_path: Path, source: str, source_url: str = "") -> None:
        digest = sha256_file(pdf_path)
        self.db.attach_pdf(
            doc_id,
            pdf_path,
            sha256=digest,
            source_url=source_url or None,
        )
        self.db.upsert_document(
            {
                "doc_id": doc_id,
                "source_url": source_url or None,
                "metadata_json": {"download_source": source},
            }
        )
        self.db.log_event(
            "INFO", "fetch", f"{doc_id} 下载成功 ({source})", doc_id=doc_id
        )

    def _download(self, url: str, target: Path, *, expect_pdf: bool) -> bool:
        target.parent.mkdir(parents=True, exist_ok=True)
        if self.downloader is not None:
            try:
                result = self.downloader(url, target)
                if isinstance(result, (bytes, bytearray)):
                    target.write_bytes(bytes(result))
                return target.exists() and (not expect_pdf or self._looks_like_pdf(target))
            except Exception as exc:
                log.info("Injected downloader failed for %s: %s", url, exc)
                return False
        try:
            import httpx

            with httpx.Client(
                timeout=self.timeout, follow_redirects=True
            ) as client:
                with client.stream("GET", url) as response:
                    response.raise_for_status()
                    with open(target, "wb") as handle:
                        for chunk in response.iter_bytes():
                            handle.write(chunk)
            if expect_pdf and not self._looks_like_pdf(target):
                target.unlink(missing_ok=True)
                return False
            return target.exists() and target.stat().st_size > 0
        except Exception as exc:
            log.info("Download failed %s: %s", url, exc)
            target.unlink(missing_ok=True)
            return False

    @staticmethod
    def _looks_like_pdf(path: Path) -> bool:
        try:
            with open(path, "rb") as handle:
                return handle.read(5) == b"%PDF-"
        except OSError:
            return False
