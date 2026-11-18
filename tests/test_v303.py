"""Unit tests for v3.0.3:
- Tolerant JSON repair for stray unquoted tokens/words before property keys (e.g. `Semi "key":`)
- Preference of outer `items` over nested `cards` in JSON payload extractor
- Human-friendly error reporting in semi-auto import
- Verifying version bump to v3.0.3
"""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from models.atomic_prompts import repair_json_text
from models.web_ai_bridge import ClipboardBatchService, _extract_json_payload


def test_v303_version_json():
    version_file = Path(__file__).resolve().parents[1] / "app_core" / "version.json"
    assert version_file.is_file()
    data = json.loads(version_file.read_text(encoding="utf-8"))
    assert data.get("version") >= "3.0.3"


def test_repair_json_stray_token_before_key():
    # 1. Accidental prefix before key like `Semi "generalized_conjecture": null`
    bad_json = """{
        "item_id": "ITEM-1",
        "result": {
            "title": "Test Paper",
            Semi "generalized_conjecture": null
        }
    }"""
    repaired = repair_json_text(bad_json)
    parsed = json.loads(repaired)
    assert parsed["item_id"] == "ITEM-1"
    assert parsed["result"]["title"] == "Test Paper"
    assert parsed["result"]["generalized_conjecture"] is None

    # 2. Accidental prefix like `Assistant: "items": [...]`
    bad_json2 = """{
        Assistant: "items": [
            {"item_id": "ITEM-2", "result": {"skip": true}}
        ]
    }"""
    repaired2 = repair_json_text(bad_json2)
    parsed2 = json.loads(repaired2)
    assert "items" in parsed2
    assert parsed2["items"][0]["item_id"] == "ITEM-2"


def test_parse_reply_with_stray_prefix_recovers_all_items():
    # Payload modeled after user's actual problematic Batch 01 reply
    payload_text = """{
    "items": [
        {
            "item_id": "DOC-PAP-0000-8C9C21-0",
            "result": {
                "document_title": "Paper 0",
                "cards": [
                    {
                        "card_type": "CONC",
                        "title_zh": "结论0",
                        "summary_zh": "这是摘要0",
                        "premises_and_conditions": "前提条件0"
                    }
                ]
            }
        },
        {
            "item_id": "DOC-PAP-0000-8C9C21-1",
            "result": {
                "document_title": "Paper 1",
                "cards": [
                    {
                        "card_type": "STRAT",
                        "title_zh": "策略1",
                        "summary_zh": "这是摘要1",
                        "premises_and_conditions": "前提条件1",
                        "source_section": "I. INTRODUCTION",
                        Semi "generalized_conjecture": null
                    }
                ]
            }
        },
        {
            "item_id": "DOC-PAP-0000-8C9C21-2",
            "result": {
                "skip": true
            }
        }
    ]
}"""
    svc = ClipboardBatchService()
    results = svc.parse_reply(payload_text)
    assert len(results) == 3
    assert "DOC-PAP-0000-8C9C21-0" in results
    assert "DOC-PAP-0000-8C9C21-1" in results
    assert "DOC-PAP-0000-8C9C21-2" in results
    assert results["DOC-PAP-0000-8C9C21-0"].cards[0].title_zh == "结论0"
    assert results["DOC-PAP-0000-8C9C21-1"].cards[0].title_zh == "策略1"
    assert results["DOC-PAP-0000-8C9C21-2"].skip is True


def test_extract_json_payload_prefers_items_over_cards():
    # If a payload has items containing cards, it MUST return the outer dict with items
    data = {
        "items": [
            {
                "item_id": "DOC-01",
                "result": {
                    "cards": [
                        {
                            "card_type": "CONC",
                            "title_zh": "Test",
                            "summary_zh": "Sum",
                            "premises_and_conditions": "Prem"
                        }
                    ]
                }
            }
        ]
    }
    extracted = _extract_json_payload(json.dumps(data))
    assert "items" in extracted
    assert extracted["items"][0]["item_id"] == "DOC-01"
