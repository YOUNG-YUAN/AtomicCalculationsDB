"""High-confidence document metadata extraction for local PDFs.

Priority:
1. Zotero/Mendeley ``Authors - Year - Title.pdf`` filename metadata;
2. DOI / PII plus Crossref/OpenAlex lookup (official title, journal, year);
3. first-page font-size layout analysis;
4. local OCR / optional VLM callback when the text layer is visibly corrupted;
5. LLM write-back (handled by the pipeline after the first extraction).

Invalid titles such as ``untitled``, ``PII: ...``, ``doi: ...`` or mojibake
fragments are never accepted.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from parser.document_profile import is_manual_document

log = logging.getLogger(__name__)

try:  # PyMuPDF ≥1.24
    import pymupdf as fitz  # type: ignore
except ImportError:  # pragma: no cover
    try:
        import fitz  # type: ignore
    except ImportError:  # pragma: no cover
        fitz = None  # type: ignore

DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"'<>]+)", re.I)
PII_RE = re.compile(r"\b\d{4}-\d{4}\(\d{2}\)\d{4,5}-[0-9Xx]\b")
YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
JOURNAL_RE = re.compile(
    r"(Phys(?:ical)?\.?\s*Rev(?:iew)?\.?\s*[A-E]?|"
    r"J(?:ournal)?\.?\s*Phys(?:ics)?\.?\s*[A-G]?|"
    r"Comput(?:er)?\.?\s*Phys(?:ics)?\.?\s*Commun(?:ications)?\.?|"
    r"At(?:omic)?\.?\s*Data\s*(?:Nucl(?:ear)?\.?\s*Data\s*Tables)?|"
    r"Atomic\s+Data\s+and\s+Nuclear\s+Data\s+Tables|"
    r"Physical\s+Review\s+[A-E]|"
    r"Journal\s+of\s+Physics\s+[A-G]|"
    r"Computer\s+Physics\s+Communications|"
    r"Astronomy\s*&\s*Astrophysics|"
    r"New\s+Journal\s+of\s+Physics|"
    r"Scientific\s+Reports|"
    r"Optics\s+Express|"
    r"Nature(?:\s+Physics)?|"
    r"Science)",
    re.I,
)
_INVALID_PREFIXES = (
    "untitled", "pii:", "doi:", "arxiv:", "http://", "https://",
    "microsoft word", "unknown", "microsoft powerpoint",
)
_MOJIBAKE_MARKERS = ("~", "!", "□", "�", "=")
ZOTERO_FILENAME_RE = re.compile(
    r"^(?P<authors>.+?)\s*[-_]\s*(?P<year>19\d\d|20\d\d)\s*[-_]\s*"
    r"(?P<title>.{12,})\.pdf$",
    re.I,
)


@dataclass
class DocumentMetadata:
    title: str = ""
    authors: list[str] = field(default_factory=list)
    journal: str = ""
    year: int | None = None
    doi: str = ""
    pii: str = ""
    source: str = ""
    confidence: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "authors": list(self.authors),
            "journal": self.journal,
            "year": self.year,
            "doi": self.doi,
            "pii": self.pii,
            "source": self.source,
            "confidence": self.confidence,
        }


def normalize_text(value: Any) -> str:
    return " ".join(str(value or "").replace("\n", " ").split()).strip()


def is_valid_title(value: Any) -> bool:
    title = normalize_text(value)
    if len(title) < 12 or len(title) > 500:
        return False
    lowered = title.lower()
    if any(lowered.startswith(prefix) for prefix in _INVALID_PREFIXES):
        return False
    if any(token in lowered for token in ("untitled", "microsoft word", "pii:")):
        return False
    if not re.search(r"[A-Za-z㐀-鿿]{3,}", title):
        return False
    letters = len(re.findall(r"[A-Za-z㐀-鿿]", title))
    compact = len(re.sub(r"\s+", "", title))
    if compact and letters / compact < 0.55:
        return False
    marker_hits = sum(title.count(marker) for marker in _MOJIBAKE_MARKERS)
    if marker_hits >= 2 and compact and letters / compact < 0.75:
        return False
    if re.search(r"(?:[A-Za-z]\s*[~!□=]\s*){2,}", title):
        return False
    return True


def parse_zotero_filename(path: str | Path) -> DocumentMetadata:
    """Parse Zotero/Mendeley ``Authors - Year - Title.pdf`` filenames."""
    name = Path(path).name
    match = ZOTERO_FILENAME_RE.match(name)
    if not match:
        return DocumentMetadata()
    title = normalize_text(match.group("title")).strip(" ._-")
    if not is_valid_title(title):
        return DocumentMetadata()
    raw_authors = normalize_text(match.group("authors"))
    authors = [
        part.strip()
        for part in re.split(r"\s*[;,]\s*|\s+and\s+", raw_authors)
        if part.strip()
    ]
    try:
        year = int(match.group("year"))
    except (TypeError, ValueError):
        year = None
    return DocumentMetadata(
        title=title,
        authors=authors or ([raw_authors] if raw_authors else []),
        year=year,
        source="zotero_filename",
        confidence=0.93,
        raw={"filename": name},
    )


def zotero_filename_metadata(path: str | Path) -> dict[str, Any]:
    return parse_zotero_filename(path).as_dict()


def strip_doi(value: str) -> str:
    return str(value or "").rstrip(".,;:)]}'\"").strip()


def extract_doi_pii(text: str) -> tuple[str, str]:
    doi_match = DOI_RE.search(text or "")
    pii_match = PII_RE.search(text or "")
    return (
        strip_doi(doi_match.group(1)) if doi_match else "",
        pii_match.group(0) if pii_match else "",
    )


def journal_and_year(text: str) -> tuple[str, int | None]:
    year_match = YEAR_RE.search(text or "")
    year = int(year_match.group(1)) if year_match else None
    journal_match = JOURNAL_RE.search(text or "")
    journal = normalize_text(journal_match.group(0)) if journal_match else ""
    return journal, year


def _layout_title(doc: Any) -> str:
    if doc is None or not getattr(doc, "page_count", 0):
        return ""
    page = doc[0]
    data = page.get_text("dict")
    blocks = []
    abstract_y = page.rect.height * 0.75
    for block in data.get("blocks", []):
        if block.get("type") != 0:
            continue
        spans = [
            span for line in block.get("lines", [])
            for span in line.get("spans", [])
            if str(span.get("text") or "").strip()
        ]
        if not spans:
            continue
        text = normalize_text(" ".join(str(span.get("text")) for span in spans))
        if not text:
            continue
        y0 = float(block.get("bbox", (0, 0, 0, 0))[1])
        size = max(float(span.get("size", 0)) for span in spans)
        if re.match(r"^abstract\b", text, re.I):
            abstract_y = min(abstract_y, y0)
            continue
        blocks.append((y0, size, text))
    if not blocks:
        return ""
    above = [item for item in blocks if item[0] < abstract_y]
    if not above:
        above = blocks
    candidates = [
        item for item in above
        if is_valid_title(item[2]) and not JOURNAL_RE.fullmatch(item[2])
    ]
    if not candidates:
        return ""
    # A multi-line title is usually split into several blocks with the same
    # leading font size.  Merge adjacent blocks around the largest one.
    anchor = max(candidates, key=lambda item: item[1])
    anchor_y, anchor_size = anchor[0], anchor[1]
    title_blocks = [
        item for item in candidates
        if item[1] >= anchor_size * 0.90
        and anchor_y - 12 <= item[0] <= anchor_y + 160
    ]
    title_blocks.sort(key=lambda item: item[0])
    merged = normalize_text(" ".join(item[2] for item in title_blocks))
    if is_valid_title(merged):
        return merged
    candidates.sort(key=lambda item: item[1], reverse=True)
    return candidates[0][2]


def _title_from_metadata(metadata: Mapping[str, Any]) -> str:
    title = normalize_text(metadata.get("title") if metadata else "")
    return title if is_valid_title(title) else ""


def _crossref_record(item: Mapping[str, Any]) -> DocumentMetadata:
    title = ""
    titles = item.get("title") or []
    if isinstance(titles, list) and titles:
        title = normalize_text(titles[0])
    authors = []
    for author in item.get("author") or []:
        name = " ".join(
            part for part in (author.get("given"), author.get("family")) if part
        )
        if name:
            authors.append(name)
    journal = ""
    containers = item.get("container-title") or []
    if isinstance(containers, list) and containers:
        journal = normalize_text(containers[0])
    year = None
    for key in ("published-print", "published-online", "issued", "created"):
        parts = ((item.get(key) or {}).get("date-parts") or [])
        if parts and parts[0]:
            try:
                year = int(parts[0][0])
                break
            except (TypeError, ValueError):
                pass
    doi = strip_doi(item.get("DOI") or "")
    return DocumentMetadata(
        title=title, authors=authors, journal=journal, year=year,
        doi=doi, source="crossref", confidence=0.99 if title else 0.0,
        raw=dict(item),
    )


def _openalex_record(item: Mapping[str, Any]) -> DocumentMetadata:
    title = normalize_text(item.get("title") or "")
    authors = []
    for authorship in item.get("authorships") or []:
        name = normalize_text((authorship.get("author") or {}).get("display_name"))
        if name:
            authors.append(name)
    journal = normalize_text(
        (((item.get("primary_location") or {}).get("source") or {}).get("display_name"))
    )
    year = item.get("publication_year")
    try:
        year = int(year) if year else None
    except (TypeError, ValueError):
        year = None
    doi = strip_doi(item.get("doi") or "")
    if doi.startswith("https://doi.org/"):
        doi = doi[len("https://doi.org/"):]
    return DocumentMetadata(
        title=title, authors=authors, journal=journal, year=year,
        doi=doi, source="openalex", confidence=0.98 if title else 0.0,
        raw=dict(item),
    )


def fetch_metadata_by_doi(doi: str, *, timeout: float = 10.0) -> DocumentMetadata:
    if not doi:
        return DocumentMetadata()
    import httpx

    headers = {"User-Agent": "AtomicCalculationDB/2.2 (mailto:local@example.invalid)"}
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client:
            response = client.get(f"https://api.crossref.org/works/{doi}")
            if response.status_code == 200:
                data = response.json().get("message") or {}
                result = _crossref_record(data)
                if result.title:
                    return result
    except Exception as exc:
        log.debug("Crossref DOI lookup failed: %s", exc)
    try:
        import httpx

        with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client:
            response = client.get(
                "https://api.openalex.org/works", params={"filter": f"doi:{doi}"}
            )
            if response.status_code == 200:
                results = (response.json() or {}).get("results") or []
                if results:
                    result = _openalex_record(results[0])
                    if result.title:
                        return result
    except Exception as exc:
        log.debug("OpenAlex DOI lookup failed: %s", exc)
    return DocumentMetadata()


def fetch_metadata_by_pii(pii: str, *, timeout: float = 10.0) -> DocumentMetadata:
    if not pii:
        return DocumentMetadata()
    import httpx

    headers = {"User-Agent": "AtomicCalculationDB/2.2 (mailto:local@example.invalid)"}
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client:
            response = client.get(
                "https://api.crossref.org/works",
                params={"query.bibliographic": pii, "rows": 5},
            )
            if response.status_code == 200:
                items = ((response.json().get("message") or {}).get("items") or [])
                for item in items:
                    # Never accept a loosely related bibliographic hit: the
                    # PII must actually appear in the Crossref record.
                    blob = json.dumps(item, ensure_ascii=False).lower()
                    if pii.lower() not in blob:
                        continue
                    result = _crossref_record(item)
                    if is_valid_title(result.title):
                        result.pii = pii
                        return result
    except Exception as exc:
        log.debug("Crossref PII lookup failed: %s", exc)
    return DocumentMetadata()


def _ocr_metadata(
    doc: Any, callback: Callable[[bytes, str], str] | None
) -> tuple[str, str, int | None]:
    if doc is None or not getattr(doc, "page_count", 0):
        return "", "", None
    try:
        page = doc[0]
        clip = fitz.Rect(0, 0, page.rect.width, page.rect.height * 0.45)
        pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=clip)
        image = pixmap.tobytes("png")
        if callback is not None:
            answer = callback(
                image,
                "Read the first page header. Return only the paper title on line 1, "
                "then journal on line 2, then publication year on line 3.",
            )
        else:
            from parser.local_ocr import local_ocr_text

            answer = local_ocr_text(image)
        lines = [
            normalize_text(line)
            for line in str(answer or "").splitlines()
            if normalize_text(line)
        ]
        title = next((line for line in lines if is_valid_title(line)), "")
        journal = ""
        year = None
        for line in lines[1:]:
            matched_journal, matched_year = journal_and_year(line)
            if matched_journal and not journal:
                journal = matched_journal
            if matched_year and year is None:
                year = matched_year
        return title, journal, year
    except Exception as exc:
        log.debug("OCR/VLM metadata extraction failed: %s", exc)
    return "", "", None


def extract_document_metadata(
    pdf_path: str | Path,
    *,
    doc: Any = None,
    allow_network: bool = True,
    metadata_fetcher: Callable[[str, str], DocumentMetadata] | None = None,
    ocr_callback: Callable[[bytes, str], str] | None = None,
) -> DocumentMetadata:
    path = Path(pdf_path)
    filename_meta = parse_zotero_filename(path)
    own_doc = doc is None
    if fitz is None or not path.exists():
        return filename_meta
    if doc is None:
        doc = fitz.open(path)
    try:
        metadata = dict(doc.metadata or {})
        texts = "\n".join(
            doc[index].get_text()
            for index in range(min(2, doc.page_count))
        )
        doi, pii = extract_doi_pii(texts + "\n" + json.dumps(metadata, ensure_ascii=False))
        journal, year = journal_and_year(texts[:6000])
        layout_title = _layout_title(doc)
        meta_title = _title_from_metadata(metadata)

        offline = DocumentMetadata(
            title=filename_meta.title or layout_title or meta_title,
            authors=list(filename_meta.authors),
            journal=journal,
            year=filename_meta.year or year,
            doi=doi,
            pii=pii,
            source=(
                "zotero_filename"
                if filename_meta.title
                else ("layout" if layout_title else ("pdf_metadata" if meta_title else ""))
            ),
            confidence=0.93 if filename_meta.title else (0.72 if layout_title else 0.45),
        )
        remote = DocumentMetadata()
        if allow_network and (doi or pii):
            if metadata_fetcher is not None:
                remote = metadata_fetcher(doi, pii)
            elif doi:
                remote = fetch_metadata_by_doi(doi)
            elif pii:
                remote = fetch_metadata_by_pii(pii)
        if remote.title and is_valid_title(remote.title):
            remote.journal = remote.journal or journal
            remote.year = remote.year or filename_meta.year or year
            remote.doi = remote.doi or doi
            remote.pii = remote.pii or pii
            remote.authors = remote.authors or list(filename_meta.authors)
            return remote

        if not offline.title or not is_valid_title(offline.title):
            ocr_title, ocr_journal, ocr_year = _ocr_metadata(doc, ocr_callback)
            if ocr_title:
                offline.title = ocr_title
                offline.journal = offline.journal or ocr_journal
                offline.year = offline.year or ocr_year
                offline.source = "ocr_vlm"
                offline.confidence = 0.65
        if offline.title and not is_valid_title(offline.title):
            offline.title = ""
        return offline
    finally:
        if own_doc and doc is not None:
            doc.close()


def repair_invalid_documents(
    db: Any,
    workspace: str | Path,
    *,
    limit: int = 200,
    allow_network: bool = True,
    metadata_fetcher: Callable[[str, str], DocumentMetadata] | None = None,
    progress: Callable[[int, int, str], None] | None = None,
    include_valid: bool = False,
    doc_ids: Iterable[str] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, int]:
    """Repair invalid records, or all Zotero-named/invalid records on demand."""
    root = Path(workspace)
    counts = {"checked": 0, "repaired": 0, "failed": 0}
    if doc_ids is not None:
        clean_ids = [str(did).strip().upper() for did in doc_ids if str(did).strip()]
        documents = [db.get_document(did) for did in clean_ids if db.get_document(did)]
    else:
        documents = db.list_documents(limit=limit)
    def _zotero_name(doc: Mapping[str, Any]) -> str:
        value = doc.get("local_pdf_path")
        if not value:
            return ""
        path = Path(str(value))
        if not path.is_absolute():
            path = root / path
        return parse_zotero_filename(path).title

    if include_valid:
        candidates = [
            doc for doc in documents
            if not is_valid_title(doc.get("title"))
            or not doc.get("pub_year")
            or not str(doc.get("journal") or "").strip()
            or not doc.get("authors_list")
            or bool(_zotero_name(doc))
        ]
    else:
        candidates = [
            doc for doc in documents
            if not is_valid_title(doc.get("title")) or not doc.get("pub_year")
        ]
    for index, document in enumerate(candidates, start=1):
        if should_stop and should_stop():
            break
        counts["checked"] += 1
        if progress:
            progress(index, len(candidates), document.get("doc_id", ""))
        try:
            value = document.get("local_pdf_path")
            pdf = Path(str(value or ""))
            if value and not pdf.is_absolute():
                pdf = root / pdf
            result = DocumentMetadata()
            if pdf.is_file():
                result = extract_document_metadata(
                    pdf, allow_network=allow_network,
                    metadata_fetcher=metadata_fetcher,
                )
            fallback_doi, fallback_pii = extract_doi_pii(
                f"{document.get('title') or ''}\n{document.get('abstract') or ''}"
            )
            if allow_network and (not result.title or not result.year):
                lookup_doi = result.doi or document.get("doi") or fallback_doi
                lookup_pii = result.pii or fallback_pii
                remote = DocumentMetadata()
                if metadata_fetcher is not None and (lookup_doi or lookup_pii):
                    remote = metadata_fetcher(lookup_doi, lookup_pii)
                elif lookup_doi:
                    remote = fetch_metadata_by_doi(lookup_doi)
                elif lookup_pii:
                    remote = fetch_metadata_by_pii(lookup_pii)
                if remote.title and is_valid_title(remote.title):
                    result.title = result.title or remote.title
                    result.journal = result.journal or remote.journal
                    result.year = result.year or remote.year
                    result.doi = result.doi or remote.doi
                    result.authors = result.authors or remote.authors
            updates: dict[str, Any] = {}
            current_title_valid = is_valid_title(document.get("title"))
            if pdf.is_file() and is_manual_document(
                title=str(document.get("title") or ""),
                doc_type=str(document.get("doc_type") or ""),
                filename=pdf.name,
            ) and str(document.get("doc_type") or "").upper() not in ("MAN", "BOK"):
                updates["doc_type"] = "MAN"
            if (
                result.title
                and is_valid_title(result.title)
                and result.title != document.get("title")
                and (
                    not current_title_valid
                    or result.source == "zotero_filename"
                    or result.source in ("crossref", "openalex")
                )
            ):
                updates["title"] = result.title
            if result.journal and not str(document.get("journal") or "").strip():
                updates["journal"] = result.journal
            if result.year and (
                not document.get("pub_year")
                or (
                    result.source == "zotero_filename"
                    and int(document.get("pub_year") or 0) != int(result.year)
                )
            ):
                updates["pub_year"] = result.year
            if result.doi and not document.get("doi"):
                updates["doi"] = result.doi
            if result.authors and (
                not document.get("authors_list")
                or result.source == "zotero_filename"
            ):
                updates["authors"] = result.authors
            if updates:
                updates["doc_id"] = document["doc_id"]
                db.upsert_document(updates)
                counts["repaired"] += 1
        except Exception:
            log.exception("Metadata repair failed for %s", document.get("doc_id"))
            counts["failed"] += 1
    if counts["repaired"]:
        db.log_event("INFO", "metadata", f"自动修复文献元数据 {counts['repaired']} 篇")
    return counts


__all__ = [
    "DocumentMetadata",
    "extract_document_metadata",
    "fetch_metadata_by_doi",
    "fetch_metadata_by_pii",
    "is_valid_title",
    "journal_and_year",
    "parse_zotero_filename",
    "repair_invalid_documents",
    "zotero_filename_metadata",
]
