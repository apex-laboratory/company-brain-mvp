"""Unit tests for app/pipeline/llm/clients.py — JSON parsing, reprompt, provider dispatch.

Mocks at the provider boundary (``get_provider()``) so no SDK or network is touched.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.pipeline.llm.clients as clients
from app.pipeline.llm.clients import LLMParseError, _parse_json, llm_json

# ── _parse_json ──────────────────────────────────────────────────────────────

def test_parse_plain_object() -> None:
    assert _parse_json('{"a": 1}') == {"a": 1}


def test_parse_strips_markdown_fences() -> None:
    assert _parse_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_parse_rejects_non_object() -> None:
    import json

    with pytest.raises(json.JSONDecodeError):
        _parse_json("[1, 2]")


def test_parse_extracts_trailing_json_after_reasoning_prose() -> None:
    # Reasoning-tier models (common on OpenRouter's free tier) narrate
    # chain-of-thought ahead of the answer even when told not to.
    text = (
        'Here\'s a thinking process:\n1. The user wants {"ok": true}.\n'
        '2. I should just output it.\n\nOutput: {"relevant": true, "reason": "policy"}'
    )
    assert _parse_json(text) == {"relevant": True, "reason": "policy"}


def test_parse_extracts_nested_object_over_inner_fragment() -> None:
    # The real answer can itself contain nested objects (decision_identifier's
    # {"decisions": [...]}) — must return the full outer object, not a
    # sub-object reachable from a later '{'.
    text = '{"decisions": [{"message_id": "m1"}, {"message_id": "m2"}]}'
    assert _parse_json(text) == {
        "decisions": [{"message_id": "m1"}, {"message_id": "m2"}]
    }


def test_parse_still_rejects_pure_prose_with_no_json() -> None:
    import json

    with pytest.raises(json.JSONDecodeError):
        _parse_json("I cannot comply with that request.")


# ── llm_json ─────────────────────────────────────────────────────────────────

def _fake_provider(call: AsyncMock) -> MagicMock:
    provider = MagicMock()
    provider.model = "test-model"
    provider.call = call
    return provider


async def test_llm_json_returns_parsed_and_usage() -> None:
    raw = AsyncMock(return_value=('{"relevant": true}', 100, 20))
    with patch.object(clients, "get_provider", return_value=_fake_provider(raw)):
        parsed, usage = await llm_json("sys", "user", stage="relevance_gate")
    assert parsed == {"relevant": True}
    assert usage.stage == "relevance_gate"
    assert usage.model == "test-model"
    assert (usage.input_tokens, usage.output_tokens) == (100, 20)


async def test_non_json_reprompts_once_and_sums_usage() -> None:
    raw = AsyncMock(side_effect=[("not json at all", 100, 10), ('{"ok": 1}', 150, 20)])
    with patch.object(clients, "get_provider", return_value=_fake_provider(raw)):
        parsed, usage = await llm_json("sys", "user", stage="t")
    assert parsed == {"ok": 1}
    assert raw.await_count == 2
    assert "not valid JSON" in raw.await_args_list[1].args[1]  # reprompted user msg
    assert (usage.input_tokens, usage.output_tokens) == (250, 30)  # both attempts


async def test_non_json_twice_raises_parse_error() -> None:
    raw = AsyncMock(side_effect=[("garbage", 1, 1), ("still garbage", 1, 1)])
    with patch.object(clients, "get_provider", return_value=_fake_provider(raw)), pytest.raises(
        LLMParseError
    ):
        await llm_json("sys", "user", stage="t")
    assert raw.await_count == 2
