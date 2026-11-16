from pathlib import Path

from config import AppConfig
from models.endpoint_pool import EndpointNode, EndpointPool


def test_new_config_has_no_legacy_single_node_sections():
    config = AppConfig({})
    assert config.get("models") is None
    assert config.get("embedding") is None
    assert config.get("reranker") is None
    assert config.get("endpoint_pool.nodes") == []


def test_legacy_gateway_values_migrate_into_pool_once():
    config = AppConfig(
        {
            "models": {
                "fast_llm": {
                    "base_url": "https://api.example/v1",
                    "api_key": "key",
                    "model": "fast-model",
                }
            },
            "endpoint_pool": {"nodes": [], "strategies": [], "role_bindings": {}},
        }
    )
    pool = EndpointPool(config)
    node = pool.nodes["legacy-fast"]
    assert node.node_type == "OPENAI_COMPATIBLE_API"
    assert node.model == "fast-model"
    role = pool.bindings["fast_llm"]
    assert role.default_strategy_id == "legacy-fast_llm-strategy"
    # No empty legacy bindings/strategies are invented for unconfigured roles.
    assert pool.bindings["slow_llm"].default_strategy_id == ""
    assert pool.bindings["vlm"].default_strategy_id == ""


def test_gateway_never_falls_back_to_legacy_model_config(tmp_path: Path):
    from models.gateway import ModelGateway

    config = AppConfig(
        {
            "models": {
                "fast_llm": {
                    "base_url": "https://legacy.example/v1",
                    "api_key": "legacy-key",
                    "model": "legacy-model",
                }
            },
            "endpoint_pool": {
                "nodes": [
                    {
                        "node_id": "unrelated",
                        "name": "无关节点",
                        "node_type": "SEMI_AUTO_CLIPBOARD",
                        "capabilities": ["TEXT_LLM"],
                    }
                ],
                "strategies": [],
                "role_bindings": {},
            },
        }
    )
    gateway = ModelGateway(config, tmp_path)
    assert gateway.fast_client.available is False
    assert gateway.fast_client.name == "fast_llm"
    assert gateway.fast_client.base_url == ""
    assert gateway.fast_client.model == ""


def test_embedding_and_reranker_nodes_drive_provider_config(tmp_path: Path):
    from models.gateway import EmbeddingProvider, RerankerProvider

    embedding_node = EndpointNode(
        node_id="cloud-embed",
        name="云端向量",
        node_type="OPENAI_COMPATIBLE_API",
        capabilities=["EMBEDDING"],
        base_url="https://embed.example/v1",
        model="text-embedding-x",
        model_dim=1536,
        api_key="key",
    )
    reranker_node = EndpointNode(
        node_id="cloud-rerank",
        name="云端重排",
        node_type="OPENAI_COMPATIBLE_API",
        capabilities=["RERANK"],
        base_url="https://rerank.example/v1",
        model="rerank-x",
        api_key="key",
    )
    embedding = EmbeddingProvider({}, tmp_path, node=embedding_node)
    reranker = RerankerProvider({}, None, node=reranker_node)
    assert embedding.provider == "cloud"
    assert embedding.model == "text-embedding-x"
    assert embedding.dimension == 1536
    assert embedding.base_url == "https://embed.example/v1"
    assert reranker.provider == "cloud"
    assert reranker.model == "rerank-x"
    assert reranker.base_url == "https://rerank.example/v1"


def test_embedding_signature_is_derived_from_pool(tmp_path: Path):
    from main_entry import _embedding_signature

    config = AppConfig(
        {
            "endpoint_pool": {
                "nodes": [
                    {
                        "node_id": "embed-node",
                        "name": "向量节点",
                        "node_type": "OPENAI_COMPATIBLE_API",
                        "capabilities": ["EMBEDDING"],
                        "base_url": "https://embed.example/v1",
                        "model": "custom-embed",
                        "model_dim": 768,
                    }
                ],
                "strategies": [
                    {
                        "strategy_id": "embed-strategy",
                        "strategy_name": "向量策略",
                        "relay_chain": ["embed-node"],
                    }
                ],
                "role_bindings": {
                    "embedding": {"default_strategy_id": "embed-strategy"}
                },
            }
        }
    )
    signature, dimension = _embedding_signature(config)
    assert signature == "custom-embed::dim=768"
    assert dimension == 768
