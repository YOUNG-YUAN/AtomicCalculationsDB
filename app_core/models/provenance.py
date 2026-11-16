"""Snapshot the role-to-model bindings used when a card is created."""

from __future__ import annotations

from typing import Any

from models.endpoint_pool import EndpointNode

ROLE_KEYS = ("fast_llm", "slow_llm", "vlm", "embedding", "reranker")


def node_snapshot(node: EndpointNode | None) -> dict[str, Any]:
    if node is None:
        return {}
    model = str(node.model or "")
    web_model = ""
    if node.node_type == "WEB_AI_PLAYWRIGHT":
        try:
            from models.web_ai_bridge import web_model_settings

            web_model = str(web_model_settings(node)[0] or "")
        except Exception:
            web_model = str(node.web_model or "")
        model = web_model or node.web_model or ""
    elif node.node_type == "SEMI_AUTO_CLIPBOARD":
        model = model or "用户手动/外部网页 AI"
    elif node.node_type == "AGENT_CLI":
        model = model or node.cli_preset or node.cli_command or "agent-cli"
    return {
        "node_id": node.node_id,
        "name": node.name,
        "node_type": node.node_type,
        "provider": (
            "web"
            if node.node_type == "WEB_AI_PLAYWRIGHT"
            else "agent_cli"
            if node.node_type == "AGENT_CLI"
            else "semi_auto"
            if node.node_type == "SEMI_AUTO_CLIPBOARD"
            else "local"
            if node.node_type == "LOCAL_ONNX"
            else "api"
        ),
        "model": model,
        "web_preset": node.web_preset,
        "web_model": web_model,
    }


def collect_pool_provenance(pool: Any) -> dict[str, dict[str, Any]]:
    if pool is None:
        return {}
    result: dict[str, dict[str, Any]] = {}
    for role in ROLE_KEYS:
        try:
            node = pool.node_for_role(role)
        except Exception:
            node = None
        snapshot = node_snapshot(node)
        if snapshot:
            result[role] = snapshot
    return result
