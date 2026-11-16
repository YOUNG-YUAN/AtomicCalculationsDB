import json
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from crawler.discovery_api import DiscoveryService, DocumentCandidate
from db.qdrant_manager import QdrantManager
from models.atomic_prompts import (
    ExtractionResult,
    KeywordDraft,
    KnowledgeCardDraft,
)
from parser.context_bundler import ContextBundle
from pipeline import Pipeline
from crawler.manual_inbox_watcher import ManualInboxWatcher
from crawler.waterfall_fetcher import WaterfallFetcher
from db.hybrid_retriever import HybridRetriever
from db.sqlite_manager import SQLiteManager, now_iso
from scheduler.morning_reporter import MorningReporter
from scheduler.night_worker import NightWorker
from sync_mcp.acdb_packager import ACDBPackager
from sync_mcp.cloud_sync import CloudSyncManager
from sync_mcp.mcp_server import KnowledgeMCPServer, build_mcp_config


@pytest.fixture()
def workspace(tmp_path: Path):
    path = tmp_path / "workspace"
    path.mkdir()
    for name in ("storage", "inbox_manual", "morning_reports"):
        (path / name).mkdir()
    db = SQLiteManager(path, embedding_signature="test::dim=4")
    yield path, db
    db.close()


# --------------------------------------------------------------- discovery
def test_discovery_parsers_and_gatekeeper():
    openalex = {
        "title": "MCDHF and RCI calculations of Fe XVII",
        "authorships": [{"author": {"display_name": "A. Author"}}],
        "primary_location": {
            "source": {"display_name": "Physical Review A"},
            "landing_page_url": "https://doi.org/10.1234/oa",
        },
        "publication_year": 2024,
        "doi": "https://doi.org/10.1234/oa",
        "abstract_inverted_index": {"We": [0], "calculate": [1], "Fe": [2], "XVII": [3]},
        "cited_by_count": 7,
        "type": "article",
    }
    candidate = DiscoveryService._parse_openalex(openalex)
    assert candidate.doi == "10.1234/oa"
    assert candidate.authors == ["A. Author"]
    assert candidate.abstract == "We calculate Fe XVII"
    assert candidate.journal == "Physical Review A"
    assert candidate.citations == 7

    crossref = {
        "title": ["Relativistic atomic structure"],
        "author": [{"given": "B.", "family": "Author"}],
        "container-title": ["Atomic Data and Nuclear Data Tables"],
        "published": {"date-parts": [[2022, 1, 1]]},
        "DOI": "10.1234/CR",
        "type": "journal-article",
        "abstract": "<jats:p>MCDHF</jats:p>",
        "is-referenced-by-count": 3,
    }
    cr = DiscoveryService._parse_crossref(crossref)
    assert cr.doi == "10.1234/cr"
    assert cr.authors == ["B. Author"]
    assert cr.abstract == "MCDHF"
    assert cr.pub_year == 2022

    service = DiscoveryService()
    accepted = service.gatekeeper.evaluate(
        "MCDHF and RCI calculations of E1 transitions in Fe XVII"
    )
    assert accepted.accepted
    rejected = service.gatekeeper.evaluate(
        "Tokamak transport simulation of a laser-produced plasma plume"
    )
    assert rejected.rejected


def test_candidate_merge_and_document_mapping():
    a = DocumentCandidate(
        title="MCDHF paper", doi="10.1234/merge", source="openalex", pub_year=2024
    )
    b = DocumentCandidate(
        title="MCDHF paper (journal)",
        arxiv_id="2401.00001",
        abstract="MCDHF and RCI",
        source="arxiv",
        authors=["A. Author"],
    )
    a.merge(b)
    assert a.arxiv_id == "2401.00001"
    assert a.abstract == "MCDHF and RCI"
    assert "openalex" in a.source and "arxiv" in a.source
    doc = a.to_document()
    assert doc["doi"] == "10.1234/merge"
    assert doc["download_status"] == "QUEUED"


# --------------------------------------------------------------- waterfall
def test_waterfall_arxiv_download(workspace):
    path, db = workspace
    doc_id = db.upsert_document(
        {
            "title": "MCDHF calculations of Fe XVII E1 transitions",
            "arxiv_id": "2401.00001",
            "pub_year": 2024,
        }
    )
    calls = []

    def downloader(url, target):
        calls.append(url)
        target.write_bytes(b"%PDF-1.4\nfake pdf\n%%EOF\n")
        return True

    fetcher = WaterfallFetcher(db, path, downloader=downloader)
    result = fetcher.fetch(doc_id)
    assert result.status == "DOWNLOADED"
    doc = db.get_document(doc_id)
    assert doc["download_status"] == "DOWNLOADED"
    assert doc["local_pdf_path"]
    assert (path / doc["local_pdf_path"]).exists()
    assert any("arxiv.org" in url for url in calls)
    assert db.list_tasks("PENDING")[0]["task_type"] == "PARSE"


def test_waterfall_rejects_and_manual_queue(workspace):
    path, db = workspace
    rejected_id = db.upsert_document(
        {
            "title": "Tokamak transport simulation of a laser-produced plasma plume",
            "doi": "10.1234/rejected",
        }
    )
    fetcher = WaterfallFetcher(db, path, downloader=lambda url, target: False)
    result = fetcher.fetch(rejected_id)
    assert result.status == "REJECTED"
    assert db.get_document(rejected_id)["download_status"] == "REJECTED"

    manual_id = db.upsert_document(
        {"title": "Atomic structure calculation without a PDF identifier"}
    )
    result = fetcher.fetch(manual_id)
    assert result.status == "MANUAL_REQUIRED"
    assert db.get_document(manual_id)["download_status"] == "MANUAL_REQUIRED"


def test_discover_and_queue(workspace):
    path, db = workspace
    candidate = DocumentCandidate(
        title="MCDHF and RCI calculations of E1 transitions in Fe XVII",
        doi="10.1234/queue",
        accepted=True,
        gatekeeper_score=80,
    )

    class FakeDiscovery:
        def discover(self, query, sources, limit_per_source):
            return [candidate]

    fetcher = WaterfallFetcher(db, path, discovery=FakeDiscovery())
    doc_ids = fetcher.discover_and_queue("Fe XVII")
    assert len(doc_ids) == 1
    assert db.get_document(doc_ids[0])["download_status"] == "QUEUED"
    assert db.list_tasks("PENDING")[0]["task_type"] == "FETCH"


# ------------------------------------------------------------ manual inbox
def test_manual_inbox_matches_queued_document(workspace, monkeypatch):
    path, db = workspace
    queued = db.upsert_document(
        {
            "title": "Queued MCDHF paper",
            "doi": "10.1234/manual",
            "journal": "Physical Review A",
            "abstract": "Original abstract",
            "download_status": "MANUAL_REQUIRED",
            "metadata_json": {"origin": "queue"},
        }
    )
    monkeypatch.setattr(
        "crawler.manual_inbox_watcher.extract_pdf_identity",
        lambda pdf: {"doi": "10.1234/manual", "title": "Queued MCDHF paper", "text": ""},
    )
    pdf = path / "inbox_manual" / "downloaded.pdf"
    pdf.write_bytes(b"%PDF-1.4\nmanual\n%%EOF\n")

    watcher = ManualInboxWatcher(db, path)
    results = watcher.scan()
    assert len(results) == 1 and results[0].doc_id == queued
    assert results[0].matched and not results[0].created
    doc = db.get_document(queued)
    assert doc["download_status"] == "DOWNLOADED"
    assert doc["journal"] == "Physical Review A"
    assert doc["abstract"] == "Original abstract"
    assert doc["metadata"]["origin"] == "queue"
    assert doc["local_pdf_path"]
    assert not pdf.exists()


def test_manual_inbox_creates_new_document_and_deduplicates(workspace, monkeypatch):
    path, db = workspace
    monkeypatch.setattr(
        "crawler.manual_inbox_watcher.extract_pdf_identity",
        lambda pdf: {"doi": None, "title": "A brand new atomic paper", "text": ""},
    )
    pdf = path / "inbox_manual" / "new.pdf"
    pdf.write_bytes(b"%PDF-1.4\nnew\n%%EOF\n")
    watcher = ManualInboxWatcher(db, path)
    first = watcher.ingest_paths([pdf])[0]
    assert first.created and first.doc_id
    second = watcher.ingest_paths([pdf])[0]
    assert second.doc_id == first.doc_id and not second.created


# --------------------------------------------------------------- scheduler
def test_night_worker_window_and_tasks(workspace):
    path, db = workspace
    config = {"night_shift": {"enabled": True, "start": "18:00", "end": "08:00"}}
    worker = NightWorker(db, workspace_dir=str(path), config=config)
    assert worker.is_in_window(datetime(2026, 10, 31, 20, 0))
    assert worker.is_in_window(datetime(2026, 11, 1, 7, 0))
    assert not worker.is_in_window(datetime(2026, 10, 31, 12, 0))

    executed = []
    worker.register_handler("FETCH", lambda task: executed.append(task["task_type"]))
    worker.register_handler("PARSE", lambda task: executed.append(task["task_type"]))
    fetch_id = db.add_task("FETCH", priority=10)
    parse_id = db.add_task("PARSE", priority=1)
    task = worker.run_once()
    assert task["task_id"] == fetch_id and task["result_status"] == "DONE"
    task = worker.run_once(exclude_types=("FETCH",))
    assert task["task_id"] == parse_id and task["result_status"] == "DONE"
    assert executed == ["FETCH", "PARSE"]


def test_morning_report(workspace):
    path, db = workspace
    now = datetime.now().astimezone()
    since = (now - timedelta(hours=2)).isoformat(timespec="seconds")
    until = (now + timedelta(hours=1)).isoformat(timespec="seconds")
    doc_id = db.upsert_document(
        {
            "title": "Manual download still needed",
            "download_status": "MANUAL_REQUIRED",
        }
    )
    card_id = db.upsert_knowledge_card(
        {
            "card_type": "STRAT",
            "title_zh": "Fe XVII 策略",
            "summary_zh": "MCDHF active space",
            "premises_and_conditions": "Ne-like",
            "supporting_doc_ids": [doc_id],
            "has_conflict": 1,
        }
    )
    reporter = MorningReporter(db, path)
    report = reporter.generate(date=now.date(), since=since, until=until)
    text = report.read_text(encoding="utf-8")
    assert report.exists()
    assert card_id in text
    assert doc_id in text
    assert "学术争议" in text


# ------------------------------------------------------------------- .acdb
def test_acdb_export_import_roundtrip(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    db = SQLiteManager(source, embedding_signature="test::dim=4")
    storage = source / "storage" / "DOC-PAP-2024-ABCDEF"
    storage.mkdir(parents=True)
    pdf = storage / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4\nroundtrip\n%%EOF\n")
    doc_id = db.upsert_document(
        {
            "doc_id": "DOC-PAP-2024-ABCDEF",
            "title": "Roundtrip paper",
            "doi": "10.1234/roundtrip",
            "pub_year": 2024,
        }
    )
    db.attach_pdf(doc_id, pdf, sha256="abc")
    card_id = db.upsert_knowledge_card(
        {
            "card_type": "STRAT",
            "title_zh": "Roundtrip card",
            "summary_zh": "Active space strategy",
            "premises_and_conditions": "Fe XVII",
            "supporting_doc_ids": [doc_id],
        }
    )
    db.add_evidence(card_id, doc_id, "⟪FOCUS⟫ evidence", page_num=3)
    kw_id = db.upsert_keyword("core-valence correlation", term_zh="芯价关联")
    db.link_card_keywords(card_id, [kw_id])

    packager = ACDBPackager(db, source)
    package = packager.export(tmp_path / "roundtrip.acdb", doc_ids=[doc_id])
    with zipfile.ZipFile(package) as archive:
        names = set(archive.namelist())
        assert "manifest.json" in names
        assert "meta_dump.jsonl" in names
        assert any(name.endswith("paper.pdf") for name in names)
    db.close()

    target = tmp_path / "target"
    target.mkdir()
    target_db = SQLiteManager(target, embedding_signature="test::dim=4")
    counts = ACDBPackager(target_db, target).import_package(package)
    assert counts["documents"] == 1
    assert counts["cards"] == 1
    assert counts["evidence"] == 1
    restored = target_db.get_document(doc_id)
    assert restored["title"] == "Roundtrip paper"
    assert restored["local_pdf_path"]
    assert (target / restored["local_pdf_path"]).exists()
    assert target_db.get_knowledge_card(card_id)["supporting_doc_ids_list"] == [doc_id]
    assert target_db.keywords_for_card(card_id)[0]["kw_id"] == kw_id
    target_db.close()


def test_pipeline_bundle_to_card_evidence_and_vector(workspace):
    path, db = workspace
    qdrant = QdrantManager(path, dimension=4, embedding_signature="test::dim=4")
    assert qdrant.available
    document_id = db.upsert_document(
        {
            "title": "Pipeline test paper",
            "doi": "10.1234/pipeline",
            "pub_year": 2024,
            "authority_weight": 1.2,
        }
    )
    bundle = ContextBundle(
        doc_id=document_id,
        section_path="II.B Active space",
        global_premise_paragraph="For Fe XVII, use GRASP2018.",
        backtraced_definitions=["Model B is defined as n=5."],
        focal_window_markdown="⟪FOCUS⟫ The gauge results agree within 1%. ⟪/FOCUS⟫",
        detected_channels=["CONCLUSION"],
        preliminary_tags_bitmask=11,
        focal_sentence="The gauge results agree within 1%.",
        page_num=4,
    )
    bundles_path = path / "storage" / document_id / "bundles.json"
    bundles_path.parent.mkdir(parents=True, exist_ok=True)
    bundles_path.write_text(
        json.dumps([bundle.model_dump()], ensure_ascii=False), encoding="utf-8"
    )

    class FakeEmbedding:
        available = True

        def embed_query(self, text):
            return [1.0, 0.0, 0.0, 0.0]

    class FakeGateway:
        embedding = FakeEmbedding()
        registry = None

        def extract_cards(self, bundle, doc_metadata=None):
            return ExtractionResult(
                cards=[
                    KnowledgeCardDraft(
                        card_type="STRAT",
                        title_zh="Fe XVII 策略",
                        summary_zh="活性空间扩展至 n=7",
                        premises_and_conditions="适用于 Ne-like Fe XVII",
                        element="Fe",
                        z_num=26,
                        ion_stage="Fe XVII",
                        isoelectronic="Ne-like",
                        method="MCDHF+RCI",
                        physical_process="E1_transition",
                        conclusion_flags=11,
                        keywords=["active space"],
                    )
                ],
                keywords=[
                    KeywordDraft(
                        term_en="core-valence correlation",
                        term_zh="芯价关联",
                        category="CORRELATION",
                    )
                ],
            )

    pipeline = Pipeline(
        db,
        path,
        config=None,
        gateway=FakeGateway(),
        qdrant=qdrant,
        fetcher=WaterfallFetcher(db, path),
    )
    task = {
        "doc_id": document_id,
        "payload_json": json.dumps(
            {"bundles_path": str(bundles_path.relative_to(path))}
        ),
    }
    assert pipeline.handle_extract(task)
    cards = db.list_knowledge_cards(limit=10)
    assert len(cards) == 1
    card = cards[0]
    assert card["supporting_doc_ids_list"] == [document_id]
    assert db.list_evidence(card["knw_id"])
    assert db.keywords_for_card(card["knw_id"])
    assert db.get_document(document_id)["parse_status"] == "LLM_EXTRACTED"
    assert qdrant.count() == 1
    qdrant.close()


# --------------------------------------------------------------- cloud sync
class FakeBackend:
    def __init__(self):
        self.files = {}
        self.manifest = None

    def upload(self, local, remote):
        self.files[remote] = Path(local).read_bytes()

    def download(self, remote, local):
        Path(local).write_bytes(self.files.get(remote, b""))

    def read_manifest(self, remote_root):
        return self.manifest

    def write_manifest(self, remote_root, manifest):
        self.manifest = dict(manifest)
        self.files[remote_root.rstrip("/") + "/manifest.json"] = json.dumps(
            manifest
        ).encode("utf-8")


def test_cloud_sync_manifest_and_push(workspace):
    path, db = workspace
    config = {"sync": {"webdav": {"enabled": True, "remote_dir": "/acdb"}}}
    backend = FakeBackend()
    sync = CloudSyncManager(db, path, config=config, webdav_backend=backend)
    manifest = sync.build_manifest()
    assert manifest["format"] == "acdb-sync/1"
    assert any(entry["path"].endswith(".sqlite") for entry in manifest["entries"])
    result = sync.sync_webdav()
    assert result.ok and result.uploaded >= 1
    assert backend.manifest is not None
    assert any("/acdb/manifest.json" == key for key in backend.files)


# -------------------------------------------------------------------- MCP
def test_mcp_server_builds(workspace):
    path, db = workspace
    db.upsert_document({"title": "MCP paper"})
    retriever = HybridRetriever(db)
    server = KnowledgeMCPServer(db, retriever)
    assert server.status()["available"] is True
    assert server.build_server() is not None
    config = build_mcp_config("C:/fake/AtomicCalculationDB.exe")
    assert config["mcpServers"]["atomic-calculation-db"]["args"] == ["--mcp"]
