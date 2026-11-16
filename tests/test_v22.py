import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from db.sqlite_manager import SQLiteManager
from models.atomic_prompts import (
    KnowledgeCardDraft,
    parse_card_revision_json,
    parse_extraction_json,
)
from models.gateway import OpenAICompatibleClient
from parser.card_quality import (
    clean_formula_field,
    is_invalid_card,
    purge_invalid_cards,
    sanitize_existing_cards,
)
from parser.metadata_extractor import (
    DocumentMetadata,
    extract_document_metadata,
    is_valid_title,
    repair_invalid_documents,
)
from parser.pdf_to_markdown import extract_pdf_identity
from ui.atomic_markdown import atomic_symbols_to_html, render_atomic_markdown


def test_atomic_markdown_symbols_and_lists():
    assert atomic_symbols_to_html("^3P_2") == "<sup>3</sup>P<sub>2</sub>"
    assert atomic_symbols_to_html("^2P_{3/2}") == "<sup>2</sup>P<sub>3/2</sub>"
    assert "1s<sup>2</sup>" in atomic_symbols_to_html("1s^2 2p^6")
    assert "Fe<sup>16+</sup>" in atomic_symbols_to_html("Fe^{16+}")
    assert "Li<sup>−</sup>" in atomic_symbols_to_html("Li-")
    rendered = render_atomic_markdown(
        "体系：Ne-like；理论：MCDHF；前置近似：Breit/QED", premises=True
    )
    assert rendered.count("<li>") == 3
    assert "<strong>" in render_atomic_markdown("**核心结论**")
    table = render_atomic_markdown(
        "| 态 | 能量 |\n| --- | --- |\n| ^3P_2 | 1.0 |"
    )
    assert "<table>" in table and "<sup>3</sup>" in table
    code = render_atomic_markdown("```python\nx = a^2\n```")
    assert "<pre" in code and "a^2" in code


def test_metadata_title_validation_and_layout(tmp_path: Path):
    pytest.importorskip("fitz")
    import pymupdf

    pdf = tmp_path / "paper.pdf"
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((60, 60), "Physical Review A 96, 012345 (2017)", fontsize=9)
    page.insert_text((60, 110), "Relativistic MCDHF calculations for Fe XVII", fontsize=18)
    page.insert_text((60, 150), "A. Author and B. Author", fontsize=10)
    page.insert_text((60, 190), "Abstract", fontsize=12)
    page.insert_text((60, 210), "We calculate Fe XVII with DOI 10.1103/PhysRevA.96.012345", fontsize=10)
    document.set_metadata({"title": "untitled", "author": "A"})
    document.save(pdf)
    document.close()

    result = extract_document_metadata(pdf, allow_network=False)
    assertresult = result.as_dict()
    assert assertresult["title"] == "Relativistic MCDHF calculations for Fe XVII"
    assert assertresult["journal"] == "Physical Review A"
    assert assertresult["year"] == 2017
    assert assertresult["doi"] == "10.1103/PhysRevA.96.012345"
    assert is_valid_title("PII: 0010-4655(89)90136-7") is False
    assert is_valid_title("C ~ g J ! 5 (") is False

    identity = extract_pdf_identity(pdf)
    assert identity["title"] == "Relativistic MCDHF calculations for Fe XVII"
    assert identity["journal"] == "Physical Review A"
    assert identity["year"] == 2017
    assert identity["doi"] == "10.1103/PhysRevA.96.012345"


def test_metadata_ocr_fallback_returns_journal_and_year(tmp_path: Path):
    pytest.importorskip("fitz")
    import pymupdf

    pdf = tmp_path / "scan.pdf"
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((60, 120), "C ~ g J ! 5 (", fontsize=18)
    document.save(pdf)
    document.close()

    def fake_ocr(image: bytes, prompt: str) -> str:
        assert image.startswith(b"\x89PNG")
        return (
            "Core-valence correlation effects in Tl II\n"
            "Computer Physics Communications\n"
            "1989"
        )

    result = extract_document_metadata(
        pdf, allow_network=False, ocr_callback=fake_ocr
    )
    assert result.title.startswith("Core-valence")
    assert result.journal == "Computer Physics Communications"
    assert result.year == 1989
    assert result.source == "ocr_vlm"


def test_metadata_remote_fetcher_and_repair(tmp_path: Path):
    pytest.importorskip("fitz")
    import pymupdf

    pdf = tmp_path / "paper.pdf"
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((60, 80), "PII: 0010-4655(89)90136-7", fontsize=10)
    page.insert_text((60, 120), "C ~ g J ! 5 (", fontsize=18)
    document.save(pdf)
    document.close()

    def fake_fetcher(doi: str, pii: str) -> DocumentMetadata:
        return DocumentMetadata(
            title="Core-valence and core-core correlation effects in Tl II and Tl III",
            journal="Computer Physics Communications",
            year=1989,
            doi=doi,
            source="crossref",
            confidence=0.99,
        )

    result = extract_document_metadata(
        pdf, allow_network=True, metadata_fetcher=fake_fetcher
    )
    assert result.title.startswith("Core-valence")
    assert result.year == 1989

    db = SQLiteManager(tmp_path / "workspace", embedding_signature="test::dim=4")
    doc_id = db.upsert_document(
        {"title": "untitled", "pub_year": 0, "local_pdf_path": str(pdf)}
    )
    counts = repair_invalid_documents(
        db, tmp_path / "workspace", metadata_fetcher=fake_fetcher
    )
    assert counts["repaired"] == 1
    repaired = db.get_document(doc_id)
    assert repaired["title"].startswith("Core-valence")
    assert repaired["pub_year"] == 1989
    db.close()


def test_repair_uses_pii_from_legacy_title_without_pdf(tmp_path: Path):
    db = SQLiteManager(tmp_path / "workspace", embedding_signature="test::dim=4")
    doc_id = db.upsert_document(
        {"title": "PII: 0010-4655(89)90136-7", "pub_year": 0}
    )
    seen = {}

    def fake_fetcher(doi: str, pii: str) -> DocumentMetadata:
        seen["pii"] = pii
        return DocumentMetadata(
            title="Core-valence and core-core correlation effects in Tl II and Tl III",
            journal="Computer Physics Communications",
            year=1989,
            doi=doi,
            pii=pii,
            source="crossref",
            confidence=0.99,
        )

    counts = repair_invalid_documents(
        db, tmp_path / "workspace", metadata_fetcher=fake_fetcher
    )
    assert counts["repaired"] == 1
    assert seen["pii"] == "0010-4655(89)90136-7"
    repaired = db.get_document(doc_id)
    assert repaired["title"].startswith("Core-valence")
    assert repaired["pub_year"] == 1989
    db.close()


def test_clear_layout_hides_widgets_before_deletion(qapp):
    from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

    from ui.flow_layout import clear_layout

    parent = QWidget()
    layout = QVBoxLayout(parent)
    child = QLabel("temporary", parent)
    layout.addWidget(child)
    parent.show()
    child.show()
    assert child.isVisible()
    clear_layout(layout)
    assert not child.isVisible()
    parent.close()


def test_formula_sanitizing_skip_and_purge(tmp_path: Path):
    formula, notes = clean_formula_field(
        "vuuut1 H = \\sum_i h_D(i)\nThe focus fragment contains a truncated explanation."
    )
    assert "vuuut" not in formula
    assert "H = \\sum_i" in formula
    assert "truncated explanation" in notes
    formula_with_prefix, _ = clean_formula_field(
        "The focus fragment contains H = \\sum_i h_D(i)"
    )
    assert formula_with_prefix.startswith("H =")
    assert is_invalid_card({"title_zh": "某结论（证据截断）"}) is True
    assert is_invalid_card({"summary_zh": "无法可靠重建具体计算策略"}) is True
    assert parse_extraction_json('{"skip": true}').skip is True

    db = SQLiteManager(tmp_path / "workspace", embedding_signature="test::dim=4")
    bad_id = db.upsert_knowledge_card(
        {
            "card_type": "CONC",
            "title_zh": "（证据截断）无法可靠重建具体计算策略",
            "summary_zh": "证据不足",
            "premises_and_conditions": "未知",
        }
    )
    good_id = db.upsert_knowledge_card(
        {
            "card_type": "MODEL",
            "title_zh": "Fe XVII 的 MCDHF 模型",
            "summary_zh": "可用于类氖离子。",
            "premises_and_conditions": "中 Z 高电荷态。",
        }
    )
    legacy_id = db.upsert_knowledge_card(
        {
            "card_type": "MODEL",
            "title_zh": "带公式伪影的旧卡片",
            "summary_zh": "旧结论。",
            "premises_and_conditions": "旧前提。",
            "formulas_latex": "vuuut1 H = \\sum_i h_D(i)",
        }
    )
    counts = purge_invalid_cards(db)
    assert counts["deleted"] == 1
    assert db.get_knowledge_card(bad_id) is None
    assert db.get_knowledge_card(good_id) is not None
    sanitize_existing_cards(db)
    cleaned = db.get_knowledge_card(legacy_id)
    assert "vuuut" not in cleaned["formulas_latex"]
    assert "H = \\sum_i" in cleaned["formulas_latex"]
    db.close()


def test_card_manual_update_and_delete_cascade(tmp_path: Path):
    db = SQLiteManager(tmp_path / "workspace", embedding_signature="test::dim=4")
    doc_id = db.upsert_document({"title": "A paper for card mutation"})
    card_id = db.upsert_knowledge_card(
        {
            "card_type": "CONC",
            "title_zh": "原始标题",
            "summary_zh": "原始结论",
            "premises_and_conditions": "原始前提",
            "supporting_doc_ids": [doc_id],
        }
    )
    db.add_evidence(card_id, doc_id, "The original evidence.", page_num=3)
    db.upsert_knowledge_card(
        {
            "knw_id": card_id,
            "title_zh": "修改后的知识卡标题",
            "summary_zh": "修改后的核心结论包含规范检查。",
            "premises_and_conditions": "修改后的前提。",
            "formulas_latex": "H = \\sum_i h_D(i)",
        }
    )
    updated = db.get_knowledge_card(card_id)
    assert updated["title_zh"] == "修改后的知识卡标题"
    assert "修改后的核心结论" in updated["search_payload"]
    assert db.delete_knowledge_card(card_id) is True
    assert db.get_knowledge_card(card_id) is None
    assert db.list_evidence(card_id) == []
    db.close()


def test_pipeline_ai_revision_preserves_evidence_and_updates_fields(tmp_path: Path):
    from pipeline import Pipeline

    db = SQLiteManager(tmp_path / "workspace", embedding_signature="test::dim=4")
    doc_id = db.upsert_document({"title": "AI revision source paper"})
    card_id = db.upsert_knowledge_card(
        {
            "card_type": "CONC",
            "title_zh": "旧标题",
            "summary_zh": "旧结论",
            "premises_and_conditions": "旧前提",
            "supporting_doc_ids": [doc_id],
            "formulas_latex": "old",
        }
    )
    db.add_evidence(card_id, doc_id, "L3 evidence about gauge convergence.", page_num=2)

    class FakeGateway:
        def __init__(self):
            self.active = []

        def set_active_document(self, doc_id):
            self.active.append(doc_id)

        def revise_card(self, card, evidence_text, instruction):
            assert "gauge convergence" in evidence_text
            assert "补全公式" in instruction
            return KnowledgeCardDraft(
                card_type="MODEL",
                title_zh="修正后的知识卡",
                summary_zh="依据证据修正后的结论。",
                premises_and_conditions="适用于类氖离子。",
                formulas_latex="H = \\sum_i h_D(i)",
                keywords=["MCDHF"],
            )

    gateway = FakeGateway()
    pipeline = Pipeline(db, tmp_path / "workspace", gateway=gateway)
    revised = pipeline.revise_card(card_id, "补全公式")
    assert revised["title_zh"] == "修正后的知识卡"
    assert revised["card_type"] == "MODEL"
    assert revised["is_user_verified"] == 1
    assert "H = \\sum_i" in revised["formulas_latex"]
    assert revised["evidence"]
    assert gateway.active[0] == doc_id and gateway.active[-1] is None
    db.close()


def test_pipeline_reindex_updates_qdrant_and_flags(tmp_path: Path):
    from pipeline import Pipeline

    db = SQLiteManager(tmp_path / "workspace", embedding_signature="test::dim=4")
    card_id = db.upsert_knowledge_card(
        {
            "card_type": "CONC",
            "title_zh": "用于向量更新的卡片",
            "summary_zh": "规范收敛。",
            "premises_and_conditions": "类氖离子。",
        }
    )

    class FakeEmbedding:
        available = True

        def embed_query(self, text):
            assert text
            return [0.1, 0.2, 0.3, 0.4]

    class FakeQdrant:
        available = True

        def __init__(self):
            self.upserts = []
            self.deleted = []

        def upsert_card(self, knw_id, vector, payload):
            self.upserts.append((knw_id, vector, payload))

        def delete(self, knw_id):
            self.deleted.append(knw_id)

    class FakeGateway:
        def __init__(self):
            self.embedding = FakeEmbedding()

        def set_active_document(self, doc_id):
            pass

    qdrant = FakeQdrant()
    pipeline = Pipeline(db, tmp_path / "workspace", gateway=FakeGateway(), qdrant=qdrant)
    pipeline.reindex_card(card_id)
    assert qdrant.upserts and qdrant.upserts[0][0] == card_id
    assert db.get_knowledge_card(card_id)["is_vectorized"] == 1
    db.close()


def test_card_revision_parser_accepts_single_card_json():
    draft = parse_card_revision_json(
        json.dumps(
            {
                "card_type": "STRAT",
                "title_zh": "新策略",
                "summary_zh": "新结论",
                "premises_and_conditions": "新前提",
                "formulas_latex": "E = mc^2",
            }
        )
    )
    assert draft.card_type == "STRAT" and draft.formulas_latex == "E = mc^2"


def test_full_release_zip_contains_program_not_user_data(tmp_path: Path):
    from scripts.make_full_release import build_full_release

    root = tmp_path / "release"
    (root / "app_core").mkdir(parents=True)
    (root / "runtime").mkdir()
    (root / "workspace" / "models").mkdir(parents=True)
    (root / "app_core" / "version.json").write_text(
        json.dumps({"version": "9.9.9"}), encoding="utf-8"
    )
    (root / "app_core" / "main_entry.py").write_text("# core\n", encoding="utf-8")
    (root / "AtomicCalculationDB.exe").write_bytes(b"MZ fake launcher")
    (root / "runtime" / "AtomicCalculationDB_Runtime.exe").write_bytes(b"MZ runtime")
    (root / "workspace" / "models" / "model.onnx").write_bytes(b"onnx")
    (root / "workspace" / "atomic_meta.sqlite").write_bytes(b"private db")
    (root / "workspace" / "storage").mkdir()
    (root / "workspace" / "storage" / "paper.pdf").write_bytes(b"%PDF private")
    output = tmp_path / "full.zip"
    build_full_release(root, output=output)

    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())
    prefix = "AtomicCalculationDB"
    assert f"{prefix}/AtomicCalculationDB.exe" in names
    assert f"{prefix}/runtime/AtomicCalculationDB_Runtime.exe" in names
    assert f"{prefix}/app_core/main_entry.py" in names
    assert f"{prefix}/app_core/version.json" in names
    assert f"{prefix}/workspace/models/model.onnx" in names
    assert f"{prefix}/workspace/README.txt" in names
    assert not any("atomic_meta.sqlite" in name for name in names)
    assert not any("paper.pdf" in name for name in names)


def test_token_columns_migration_and_accumulation(tmp_path: Path):
    db = SQLiteManager(tmp_path / "workspace", embedding_signature="test::dim=4")
    doc_id = db.upsert_document({"title": "Token test paper"})
    db.add_token_usage(doc_id, 1200, 300)
    db.add_token_usage(doc_id, 800, 100)
    doc = db.get_document(doc_id)
    assert doc["input_tokens"] == 2000
    assert doc["output_tokens"] == 400
    stats = db.stats()
    assert stats["input_tokens"] == 2000
    assert stats["output_tokens"] == 400
    assert stats["total_tokens"] == 2400
    db.close()


def test_openai_client_token_callback():
    events = []

    class FakeCompletions:
        def create(self, **kwargs):
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="result"))],
                usage=SimpleNamespace(prompt_tokens=12, completion_tokens=7),
            )

    class FakeClient:
        chat = SimpleNamespace(completions=FakeCompletions())

    client = OpenAICompatibleClient(
        name="fast_llm",
        base_url="http://local/v1",
        api_key="x",
        model="test",
        token_callback=lambda *args: events.append(args),
    )
    client._openai = lambda: FakeClient()
    assert client.chat([{"role": "user", "content": "hello"}]) == "result"
    assert events and events[0][0] == "fast_llm"
    assert events[0][1] == 12 and events[0][2] == 7
