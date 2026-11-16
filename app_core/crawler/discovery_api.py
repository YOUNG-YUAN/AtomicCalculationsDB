"""Citation snowball discovery across OpenAlex / Crossref / arXiv / S2.

All network calls are injectable (``http_client``) and failure-tolerant: a
source that times out simply contributes zero candidates.  Metadata is merged
by DOI → arXiv ID → normalised title.
"""

from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping
from urllib.parse import quote

from crawler.title_gatekeeper import GatekeeperResult, TitleGatekeeper
from db.id_generator import normalize_doi, normalize_title

log = logging.getLogger(__name__)

_OPENALEX = "https://api.openalex.org"
_CROSSREF = "https://api.crossref.org"
_ARXIV = "http://export.arxiv.org/api/query"
_S2 = "https://api.semanticscholar.org/graph/v1"
_HTML_RE = re.compile(r"<[^>]+>")


def _strip_html(text: Any) -> str:
    return " ".join(_HTML_RE.sub(" ", str(text or "")).split())


def _invert_abstract(index: Any) -> str:
    if not isinstance(index, Mapping):
        return ""
    positions: dict[int, str] = {}
    for word, places in index.items():
        for place in places or []:
            try:
                positions[int(place)] = str(word)
            except (TypeError, ValueError):
                continue
    return " ".join(positions[key] for key in sorted(positions))


def _authors(values: Any) -> list[str]:
    result: list[str] = []
    for item in values or []:
        if isinstance(item, Mapping):
            name = (
                item.get("display_name")
                or item.get("name")
                or " ".join(
                    part
                    for part in (item.get("given"), item.get("family"))
                    if part
                )
            )
        else:
            name = str(item)
        name = " ".join(str(name or "").split())
        if name:
            result.append(name)
    return result


def _year(value: Any) -> int | None:
    if isinstance(value, Mapping):
        parts = value.get("date-parts") or value.get("date_parts")
        if parts and parts[0]:
            value = parts[0][0]
    match = re.search(r"(19|20)\d{2}", str(value or ""))
    return int(match.group(0)) if match else None


@dataclass
class DocumentCandidate:
    title: str
    authors: list[str] = field(default_factory=list)
    journal: str = ""
    pub_year: int | None = None
    doi: str = ""
    arxiv_id: str = ""
    abstract: str = ""
    source_url: str = ""
    source: str = ""
    doc_type: str = "PAP"
    citations: int = 0
    keywords: list[str] = field(default_factory=list)
    gatekeeper_score: int = 0
    authority_weight: float = 1.0
    accepted: bool = True
    rejected: bool = False
    gatekeeper_reasons: list[str] = field(default_factory=list)

    def key(self) -> str:
        if self.doi:
            return f"doi:{normalize_doi(self.doi)}"
        if self.arxiv_id:
            return f"arxiv:{self.arxiv_id.lower()}"
        return f"title:{normalize_title(self.title)}"

    def merge(self, other: "DocumentCandidate") -> "DocumentCandidate":
        for field_name in (
            "title",
            "journal",
            "pub_year",
            "doi",
            "arxiv_id",
            "abstract",
            "source_url",
        ):
            if not getattr(self, field_name) and getattr(other, field_name):
                setattr(self, field_name, getattr(other, field_name))
        self.authors = list(dict.fromkeys(self.authors + other.authors))
        self.keywords = list(dict.fromkeys(self.keywords + other.keywords))
        self.citations = max(self.citations, other.citations)
        sources = set(filter(None, (self.source, other.source)))
        self.source = ",".join(sorted(sources))
        if self.gatekeeper_score < other.gatekeeper_score:
            self.gatekeeper_score = other.gatekeeper_score
        self.authority_weight = max(self.authority_weight, other.authority_weight)
        self.accepted = self.accepted or other.accepted
        self.rejected = self.rejected and other.rejected
        return self

    def to_document(self) -> dict[str, Any]:
        return {
            "doc_type": self.doc_type,
            "title": self.title,
            "authors": self.authors,
            "journal": self.journal,
            "pub_year": self.pub_year,
            "doi": normalize_doi(self.doi) or None,
            "arxiv_id": self.arxiv_id or None,
            "abstract": self.abstract,
            "source_url": self.source_url,
            "gatekeeper_score": self.gatekeeper_score,
            "authority_weight": self.authority_weight,
            "download_status": "REJECTED" if self.rejected else "QUEUED",
            "metadata_json": {
                "discovery": {
                    "source": self.source,
                    "citations": self.citations,
                    "keywords": self.keywords,
                    "gatekeeper_reasons": self.gatekeeper_reasons,
                }
            },
        }


class DiscoveryService:
    def __init__(
        self,
        *,
        http_client: Any = None,
        gatekeeper: TitleGatekeeper | None = None,
        user_agent: str = "AtomicCalculationDB/2.0 (research knowledge base)",
        timeout: float = 40.0,
        mailto: str = "",
    ):
        self._client = http_client
        self.gatekeeper = gatekeeper or TitleGatekeeper()
        self.user_agent = user_agent
        self.timeout = timeout
        self.mailto = mailto

    def _client_or_create(self):
        if self._client is not None:
            return self._client
        import httpx

        self._client = httpx.Client(
            timeout=self.timeout,
            headers={"User-Agent": self.user_agent},
            follow_redirects=True,
        )
        return self._client

    def close(self) -> None:
        if self._client is not None and hasattr(self._client, "close"):
            try:
                self._client.close()
            except Exception:
                pass
        self._client = None

    def _get_json(self, url: str, params: Mapping[str, Any] | None = None) -> Any:
        client = self._client_or_create()
        for attempt in range(2):
            try:
                response = client.get(url, params=dict(params or {}))
                if hasattr(response, "raise_for_status"):
                    response.raise_for_status()
                return response.json()
            except Exception as exc:
                log.info("Discovery request failed (%s, attempt %s): %s", url, attempt + 1, exc)
                time.sleep(0.5 * (attempt + 1))
        return {}

    def _get_text(self, url: str, params: Mapping[str, Any] | None = None) -> str:
        client = self._client_or_create()
        try:
            response = client.get(url, params=dict(params or {}))
            if hasattr(response, "raise_for_status"):
                response.raise_for_status()
            return response.text
        except Exception as exc:
            log.info("Discovery text request failed (%s): %s", url, exc)
            return ""

    # ------------------------------------------------------------- OpenAlex
    def search_openalex(self, query: str, limit: int = 20) -> list[DocumentCandidate]:
        params: dict[str, Any] = {
            "search": query,
            "per-page": max(1, min(limit, 200)),
        }
        if self.mailto:
            params["mailto"] = self.mailto
        payload = self._get_json(_OPENALEX + "/works", params)
        return [
            candidate
            for item in (payload.get("results") or [])
            if (candidate := self._parse_openalex(item))
        ]

    @staticmethod
    def _parse_openalex(item: Mapping[str, Any]) -> DocumentCandidate | None:
        title = " ".join(str(item.get("title") or item.get("display_name") or "").split())
        if not title:
            return None
        location = item.get("primary_location") or {}
        source = (location.get("source") or {}) if isinstance(location, Mapping) else {}
        doi = normalize_doi(item.get("doi"))
        arxiv_id = ""
        ids = item.get("ids") or {}
        if isinstance(ids, Mapping):
            arxiv_id = str(ids.get("arxiv") or "").split("/")[-1]
        work_type = str(item.get("type") or "").lower()
        doc_type = "REV" if work_type == "review" else "BOK" if work_type == "book" else "PAP"
        return DocumentCandidate(
            title=title,
            authors=_authors(
                (entry.get("author") or {}) for entry in (item.get("authorships") or [])
            ),
            journal=str(source.get("display_name") or ""),
            pub_year=_year(item.get("publication_year")),
            doi=doi,
            arxiv_id=arxiv_id,
            abstract=_invert_abstract(item.get("abstract_inverted_index")),
            source_url=str(location.get("landing_page_url") or item.get("id") or ""),
            source="openalex",
            doc_type=doc_type,
            citations=int(item.get("cited_by_count") or 0),
            keywords=[
                str(concept.get("display_name"))
                for concept in (item.get("concepts") or [])
                if concept.get("display_name")
            ][:12],
        )

    # ------------------------------------------------------------- Crossref
    def search_crossref(self, query: str, limit: int = 20) -> list[DocumentCandidate]:
        params: dict[str, Any] = {
            "query": query,
            "rows": max(1, min(limit, 100)),
            "select": "DOI,title,author,container-title,published,abstract,type,is-referenced-by-count",
        }
        if self.mailto:
            params["mailto"] = self.mailto
        payload = self._get_json(_CROSSREF + "/works", params)
        items = (payload.get("message") or {}).get("items") or []
        return [
            candidate
            for item in items
            if (candidate := self._parse_crossref(item))
        ]

    @staticmethod
    def _parse_crossref(item: Mapping[str, Any]) -> DocumentCandidate | None:
        titles = item.get("title") or []
        title = " ".join(str(titles[0] if titles else "").split())
        if not title:
            return None
        containers = item.get("container-title") or []
        work_type = str(item.get("type") or "").lower()
        doc_type = (
            "REV" if "review" in work_type
            else "BOK" if work_type in ("book", "monograph")
            else "PAP"
        )
        doi = normalize_doi(item.get("DOI"))
        return DocumentCandidate(
            title=title,
            authors=_authors(item.get("author") or []),
            journal=str(containers[0] if containers else ""),
            pub_year=_year(item.get("published") or item.get("issued")),
            doi=doi,
            abstract=_strip_html(item.get("abstract")),
            source_url=f"https://doi.org/{doi}" if doi else "",
            source="crossref",
            doc_type=doc_type,
            citations=int(item.get("is-referenced-by-count") or 0),
        )

    # ---------------------------------------------------------------- arXiv
    def search_arxiv(self, query: str, limit: int = 20) -> list[DocumentCandidate]:
        params = {
            "search_query": f"all:{query}",
            "start": 0,
            "max_results": max(1, min(limit, 100)),
        }
        text = self._get_text(_ARXIV, params)
        if not text:
            return []
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            return []
        namespace = {"a": "http://www.w3.org/2005/Atom"}
        results: list[DocumentCandidate] = []
        for entry in root.findall("a:entry", namespace):
            title = " ".join(
                (entry.findtext("a:title", default="", namespaces=namespace) or "").split()
            )
            if not title:
                continue
            identifier = entry.findtext("a:id", default="", namespaces=namespace) or ""
            arxiv_id = identifier.rsplit("/", 1)[-1]
            authors = [
                author.findtext("a:name", default="", namespaces=namespace) or ""
                for author in entry.findall("a:author", namespace)
            ]
            published = entry.findtext("a:published", default="", namespaces=namespace)
            results.append(
                DocumentCandidate(
                    title=title,
                    authors=[name for name in authors if name],
                    journal=entry.findtext("a:journal_ref", default="", namespaces=namespace) or "",
                    pub_year=_year(published),
                    arxiv_id=arxiv_id,
                    abstract=" ".join(
                        (entry.findtext("a:summary", default="", namespaces=namespace) or "").split()
                    ),
                    source_url=identifier,
                    source="arxiv",
                )
            )
        return results

    # -------------------------------------------------------- Semantic Scholar
    def search_semantic_scholar(self, query: str, limit: int = 20) -> list[DocumentCandidate]:
        params = {
            "query": query,
            "limit": max(1, min(limit, 100)),
            "fields": "title,authors,year,venue,externalIds,abstract,citationCount,url",
        }
        payload = self._get_json(_S2 + "/paper/search", params)
        results: list[DocumentCandidate] = []
        for item in payload.get("data") or []:
            title = " ".join(str(item.get("title") or "").split())
            if not title:
                continue
            external = item.get("externalIds") or {}
            results.append(
                DocumentCandidate(
                    title=title,
                    authors=_authors(item.get("authors") or []),
                    journal=str(item.get("venue") or ""),
                    pub_year=_year(item.get("year")),
                    doi=normalize_doi(external.get("DOI")),
                    arxiv_id=str(external.get("ArXiv") or ""),
                    abstract=str(item.get("abstract") or ""),
                    source_url=str(item.get("url") or ""),
                    source="semantic_scholar",
                    citations=int(item.get("citationCount") or 0),
                )
            )
        return results

    # --------------------------------------------------------------- combine
    def discover(
        self,
        query: str,
        *,
        sources: Iterable[str] = ("openalex", "crossref", "arxiv", "semantic_scholar"),
        limit_per_source: int = 20,
        apply_gatekeeper: bool = True,
    ) -> list[DocumentCandidate]:
        merged: dict[str, DocumentCandidate] = {}
        for source in sources:
            try:
                if source == "openalex":
                    candidates = self.search_openalex(query, limit_per_source)
                elif source == "crossref":
                    candidates = self.search_crossref(query, limit_per_source)
                elif source == "arxiv":
                    candidates = self.search_arxiv(query, limit_per_source)
                elif source in ("semantic_scholar", "s2"):
                    candidates = self.search_semantic_scholar(query, limit_per_source)
                else:
                    continue
            except Exception as exc:
                log.info("Discovery source %s failed: %s", source, exc)
                continue
            for candidate in candidates:
                key = candidate.key()
                if key in merged:
                    merged[key].merge(candidate)
                else:
                    merged[key] = candidate
        results = list(merged.values())
        if apply_gatekeeper:
            for candidate in results:
                gate = self.gatekeeper.evaluate(
                    candidate.title,
                    abstract=candidate.abstract,
                    keywords=candidate.keywords,
                    journal=candidate.journal,
                    doc_type=candidate.doc_type,
                )
                candidate.gatekeeper_score = gate.score
                candidate.authority_weight = gate.authority_weight
                candidate.accepted = gate.accepted
                candidate.rejected = gate.rejected
                candidate.gatekeeper_reasons = list(gate.reasons)
        return results

    # --------------------------------------------------------------- snowball
    def snowball(
        self,
        seed_dois: Iterable[str],
        *,
        limit: int = 50,
        direction: str = "both",
    ) -> list[DocumentCandidate]:
        """One-hop reference/citation snowball via OpenAlex."""
        seed_ids: set[str] = set()
        for doi in seed_dois:
            normalized = normalize_doi(doi)
            if not normalized:
                continue
            payload = self._get_json(
                f"{_OPENALEX}/works/https://doi.org/{quote(normalized, safe='')}"
            )
            if not payload:
                continue
            if direction in ("backward", "both"):
                for reference in payload.get("referenced_works") or []:
                    seed_ids.add(str(reference).rsplit("/", 1)[-1])
            if direction in ("forward", "both") and payload.get("cited_by_api_url"):
                citations = self._get_json(
                    payload["cited_by_api_url"], {"per-page": max(1, min(limit, 100))}
                )
                for item in citations.get("results") or []:
                    seed_ids.add(str(item.get("id", "")).rsplit("/", 1)[-1])
        seed_ids.discard("")
        candidates: list[DocumentCandidate] = []
        ids = sorted(seed_ids)
        for index in range(0, len(ids), 50):
            batch = ids[index : index + 50]
            payload = self._get_json(
                _OPENALEX + "/works",
                {
                    "filter": "openalex_id:" + "|".join(batch),
                    "per-page": min(50, limit),
                },
            )
            for item in payload.get("results") or []:
                candidate = self._parse_openalex(item)
                if candidate:
                    candidates.append(candidate)
                if len(candidates) >= limit:
                    break
            if len(candidates) >= limit:
                break
        return candidates
