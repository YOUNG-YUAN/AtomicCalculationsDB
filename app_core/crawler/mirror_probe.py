"""Connectivity probe for user-configured Level-3 fallback mirrors.

No mirror domain is bundled or hardcoded.  The GUI only tests entries the user
explicitly typed into ``workspace/config.yaml``.
"""

from __future__ import annotations

import time
from typing import Any, Iterable
from urllib.parse import urlparse


def normalize_mirror(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "https://" + text
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def probe_mirrors(
    mirrors: Iterable[str],
    *,
    timeout: float = 8.0,
    client: Any = None,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    urls = [normalize_mirror(item) for item in mirrors]
    urls = [item for item in urls if item]
    if not urls:
        return results
    owns_client = client is None
    if owns_client:
        import httpx

        client = httpx.Client(
            follow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": "AtomicCalculationDB/2.1 mirror-check"},
        )
    try:
        for url in urls:
            started = time.perf_counter()
            try:
                response = client.get(url, headers={"Range": "bytes=0-0"})
                status = int(getattr(response, "status_code", 0) or 0)
                ok = status < 500
                results.append(
                    {
                        "mirror": url,
                        "ok": ok,
                        "status": status,
                        "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                        "error": "" if ok else f"HTTP {status}",
                    }
                )
            except Exception as exc:
                results.append(
                    {
                        "mirror": url,
                        "ok": False,
                        "status": 0,
                        "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                        "error": str(exc)[:200],
                    }
                )
    finally:
        if owns_client:
            try:
                client.close()
            except Exception:
                pass
    return results


__all__ = ["normalize_mirror", "probe_mirrors"]
