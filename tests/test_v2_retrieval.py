from pathlib import Path

import pytest

from db.hybrid_retriever import HybridRetriever, parse_intent
from db.qdrant_manager import QdrantManager
from db.sqlite_manager import SQLiteManager
from models.atomic_prompts import (
    KnowledgeCardDraft,
    build_extraction_messages,
    parse_extraction_json,
)
from models.gateway import FastLLM, ModelGateway
from parser.context_bundler import ContextBundle


class FakeEmbedder:
    available = True

    def embed_query(self, text: str):
        return [1.0, 0.0, 0.0, 0.0]

    def embed(self, texts):
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


class FakeReranker:
    def rerank(self, query: str, documents):
        return [0.9 if "MCDHF" in document else 0.2 for document in documents]


@pytest.fixture()
def corpus(tmp_path: Path):
    workspace = tmp_path / "workspace"
    db = SQLiteManager(workspace, embedding_signature="fake::dim=4")
    doc = db.upsert_document(
        {
            "doc_type": "PAP",
            "title": "Relativistic MCDHF calculations for Fe XVII",
            "doi": "10.1234/v2.retrieval.001",
            "pub_year": 2024,
            "authors": ["A. Author"],
            "journal": "Physical Review A",
            "abstract": "MCDHF and RCI with Breit interaction",
        }
    )
    common = {
        "element": "Fe",
        "z_num": 26,
        "ion_stage": "Fe XVII",
        "isoelectronic": "Ne-like",
        "method": "MCDHF",
        "physical_process": "E1_transition",
        "supporting_doc_ids": [doc],
    }
    master = db.upsert_knowledge_card(
        {
            **common,
            "card_level": "L1",
            "card_type": "MASTER",
            "title_zh": "类氖离子 MCDHF 总纲",
            "summary_zh": "类氖等电子序列 MCDHF 计算总纲，包含活性空间与收敛边界。",
            "premises_and_conditions": "适用于 Ne-like 离子；需检查 Breit 与 QED。",
            "search_payload": "MASTER Ne-like MCDHF overview active space convergence",
        }
    )
    card_a = db.upsert_knowledge_card(
        {
            **common,
            "card_level": "L2",
            "card_type": "STRAT",
            "title_zh": "Fe XVII MCDHF 活性空间策略",
            "summary_zh": "逐层扩展 active space 至 n=7，检查 length/velocity 规范收敛。",
            "premises_and_conditions": "适用于 Ne-like Fe XVII，含 Breit 相互作用。",
            "search_payload": "Fe XVII MCDHF active space strategy convergence length velocity gauge",
            "conclusion_flags": 11,
            "consensus_count": 2,
            "authority_score": 1.0,
            "recency_year": 2024,
        }
    )
    card_b = db.upsert_knowledge_card(
        {
            **common,
            "card_level": "L2",
            "card_type": "MODEL",
            "title_zh": "Fe XVII MCDHF 与 Dirac-Coulomb-Breit 模型",
            "summary_zh": "MCDHF 优化径向轨道，RCI 处理 active space 关联与 Breit 修正。",
            "premises_and_conditions": "适用于中 Z 类氖离子；QED 作微扰。",
            "search_payload": "Fe XVII MCDHF Dirac Coulomb Breit model active space convergence QED",
            "conclusion_flags": 4,
            "consensus_count": 8,
            "authority_score": 1.5,
            "recency_year": 2022,
        }
    )
    card_c = db.upsert_knowledge_card(
        {
            "card_level": "L2",
            "card_type": "STRAT",
            "title_zh": "W 离子 RMBPT 策略",
            "summary_zh": "高电荷态钨离子采用 RMBPT 二阶截断。",
            "premises_and_conditions": "适用于高 Z 少电子体系。",
            "element": "W",
            "z_num": 74,
            "ion_stage": "W LXXIV",
            "method": "RMBPT",
            "physical_process": "energy_levels",
            "supporting_doc_ids": [doc],
            "search_payload": "W tungsten RMBPT energy levels high Z",
            "authority_score": 1.2,
            "recency_year": 2020,
        }
    )
    qdrant = QdrantManager(
        workspace, dimension=4, embedding_signature="fake::dim=4"
    )
    assert qdrant.available
    vectors = {
        card_a: [1.0, 0.0, 0.0, 0.0],
        card_b: [0.9, 0.1, 0.0, 0.0],
        card_c: [0.0, 0.0, 1.0, 0.0],
    }
    for knw_id, vector in vectors.items():
        card = db.get_knowledge_card(knw_id)
        qdrant.upsert_card(
            knw_id,
            vector,
            {
                "card_level": card["card_level"],
                "card_type": card["card_type"],
                "element": card.get("element"),
                "z_num": card.get("z_num"),
                "isoelectronic": card.get("isoelectronic"),
                "method": card.get("method"),
                "physical_process": card.get("physical_process"),
            },
        )
    retriever = HybridRetriever(
        db, qdrant=qdrant, embedder=FakeEmbedder(), reranker=FakeReranker()
    )
    yield db, qdrant, retriever, master, card_a, card_b, card_c, doc
    qdrant.close()
    db.close()


def test_query_intent_physics_coordinates():
    intent = parse_intent("Fe XVII MCDHF E1 transitions in Ne-like ions")
    assert intent.filters["element"] == "Fe"
    assert intent.filters["z_num"] == 26
    assert intent.filters["ion_stage"] == "Fe XVII"
    assert intent.filters["isoelectronic"] == "Ne-like"
    assert intent.filters["method"] == "MCDHF"
    assert intent.filters["physical_process"] == "E1_transition"
    assert intent.chip_labels()

    chinese = parse_intent("类氖 Fe XVII 的 MCDHF 跃迁")
    assert chinese.filters["isoelectronic"] == "Ne-like"
    assert chinese.filters["element"] == "Fe"
    assert chinese.filters["method"] == "MCDHF"
    assert chinese.filters["physical_process"] == "transition"


def test_hybrid_retrieval_fuses_and_pins_master(corpus):
    db, qdrant, retriever, master, card_a, card_b, card_c, doc = corpus
    hits = retriever.search("active space convergence", limit=10)
    ids = [hit.id for hit in hits]
    assert card_a in ids and card_b in ids
    assert hits[0].id == master
    assert hits[0].kind == "master"
    assert all("final" in hit.scores for hit in hits)
    assert hits[0].scores["rerank"] >= 0.0


def test_hybrid_metadata_prefilter(corpus):
    db, qdrant, retriever, master, card_a, card_b, card_c, doc = corpus
    hits = retriever.search("Fe XVII MCDHF 策略", limit=10)
    assert [hit.id for hit in hits] == [card_a]
    hits_w = retriever.search("W RMBPT energy levels", limit=10)
    assert hits_w[0].id == card_c
    hits_flags = retriever.search("MCDHF 量化", limit=10)
    assert hits_flags and all(
        int(hit.card.get("conclusion_flags") or 0) & 1 for hit in hits_flags
    )


def test_exact_id_lookup(corpus):
    db, qdrant, retriever, master, card_a, card_b, card_c, doc = corpus
    assert retriever.search(master)[0].kind == "master"
    assert retriever.search(card_a)[0].kind == "card"
    document_hit = retriever.search(doc)[0]
    assert document_hit.kind == "document"
    assert document_hit.title.startswith("Relativistic MCDHF")


def test_fts_only_fallback(corpus):
    db, qdrant, retriever, master, card_a, card_b, card_c, doc = corpus
    fallback = HybridRetriever(db)
    hits = fallback.search("active space", limit=10)
    assert hits
    assert any(hit.id in (card_a, card_b) for hit in hits)


def test_prompt_build_and_parse():
    bundle = ContextBundle(
        doc_id="DOC-PAP-2024-ABCDEF",
        section_path="II.B",
        global_premise_paragraph="For Fe XVII...",
        focal_window_markdown="⟪FOCUS⟫ test ⟪/FOCUS⟫",
        detected_channels=["CONCLUSION"],
        preliminary_tags_bitmask=11,
        focal_sentence="test",
    )
    messages = build_extraction_messages(bundle)
    assert messages[0]["role"] == "system"
    assert "search_payload" in messages[1]["content"]
    payload = """```json
    {"cards":[{"card_type":"STRAT","title_zh":"策略","summary_zh":"结论",
    "premises_and_conditions":"Ne-like"}],"keywords":[]}
    ```"""
    parsed = parse_extraction_json(payload)
    assert parsed.cards[0].card_type == "STRAT"
    assert parsed.cards[0].premises_and_conditions == "Ne-like"

    class FakeClient:
        def chat_json(self, messages, **kwargs):
            return {
                "cards": [
                    {
                        "card_type": "MODEL",
                        "title_zh": "模型",
                        "summary_zh": "总结",
                        "premises_and_conditions": "边界",
                    }
                ],
                "keywords": [],
            }

    result = FastLLM(FakeClient()).extract_cards(bundle)
    assert result.cards[0].card_type == "MODEL"


def test_gateway_health_without_endpoints(qapp, tmp_path: Path):
    config = {
        "models": {
            "fast_llm": {"base_url": "", "model": "", "api_key": ""},
            "slow_llm": {"base_url": "", "model": "", "api_key": ""},
            "vlm": {"base_url": "", "model": "", "api_key": ""},
        },
        "embedding": {"provider": "none", "model": "BAAI/bge-m3", "dim": 1024},
        "reranker": {"provider": "none", "model": ""},
    }
    gateway = ModelGateway(config, tmp_path / "workspace")
    health = gateway.health()
    assert len(health) == 6  # fast_llm, slow_llm, audit_llm, vlm, embedding, reranker
    assert all(not item["available"] for item in health)
    assert gateway.embedding_signature == "BAAI/bge-m3::dim=1024"
