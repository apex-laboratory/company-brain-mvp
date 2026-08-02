"""Unit tests for app/pipeline/llm/clients.py — JSON parsing, reprompt, key guard.

Mocks at the raw-call boundary (``_gemini_call``) so no SDK or network is touched.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import app.pipeline.llm.clients as clients
from app.pipeline.llm.clients import LLMParseError, _parse_json, gemini_json

# ── _parse_json ──────────────────────────────────────────────────────────────

def test_parse_plain_object() -> None:
    assert _parse_json('{"a": 1}') == {"a": 1}


def test_parse_strips_markdown_fences() -> None:
    assert _parse_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_parse_rejects_non_object() -> None:
    import json

    with pytest.raises(json.JSONDecodeError):
        _parse_json("[1, 2]")


# ── gemini_json ──────────────────────────────────────────────────────────────

async def test_gemini_json_returns_parsed_and_usage() -> None:
    raw = AsyncMock(return_value=('{"relevant": true}', 100, 20))
    with patch.object(clients, "_gemini_call", raw):
        parsed, usage = await gemini_json("sys", "user", stage="relevance_gate")
    assert parsed == {"relevant": True}
    assert usage.stage == "relevance_gate"
    assert (usage.input_tokens, usage.output_tokens) == (100, 20)
    assert usage.cost_usd > 0


async def test_non_json_reprompts_once_and_sums_usage() -> None:
    raw = AsyncMock(side_effect=[("not json at all", 100, 10), ('{"ok": 1}', 150, 20)])
    with patch.object(clients, "_gemini_call", raw):
        parsed, usage = await gemini_json("sys", "user", stage="t")
    assert parsed == {"ok": 1}
    assert raw.await_count == 2
    assert "not valid JSON" in raw.await_args_list[1].args[1]  # reprompted user msg
    assert (usage.input_tokens, usage.output_tokens) == (250, 30)  # both attempts


async def test_non_json_twice_raises_parse_error() -> None:
    raw = AsyncMock(side_effect=[("garbage", 1, 1), ("still garbage", 1, 1)])
    with patch.object(clients, "_gemini_call", raw), pytest.raises(LLMParseError):
        await gemini_json("sys", "user", stage="t")
    assert raw.await_count == 2


# ── key guard ────────────────────────────────────────────────────────────────

def test_missing_key_raises_at_first_use(monkeypatch) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr(clients, "_gemini_client", None)
    # settings is a frozen pydantic model — swap the module reference instead.
    monkeypatch.setattr(clients, "settings", SimpleNamespace(gemini_api_key=""))
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        clients.gemini_client()
