"""Unit tests for the M2 LLM stages, mocked at the app.pipeline.llm.clients boundary."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.pipeline.authority import AuthorityAnnotation
from app.pipeline.stages import decision_identifier, relevance_gate, skill_extractor
from app.pipeline.types import DecisionMoment, StageUsage

_USAGE = StageUsage(stage="s", model="m", input_tokens=1, output_tokens=1, cost_usd=0.0)


# ── relevance_gate ────────────────────────────────────────────────────────────

async def test_relevance_gate_true() -> None:
    with patch.object(
        relevance_gate, "groq_json",
        AsyncMock(return_value=({"relevant": True, "reason": "policy"}, _USAGE)),
    ):
        relevant, reason, usage = await relevance_gate.is_relevant("text", "slack")
    assert relevant is True
    assert reason == "policy"
    assert usage is _USAGE


async def test_relevance_gate_false() -> None:
    with patch.object(
        relevance_gate, "groq_json",
        AsyncMock(return_value=({"relevant": False, "reason": "chit-chat"}, _USAGE)),
    ):
        relevant, _, _ = await relevance_gate.is_relevant("hi", "slack")
    assert relevant is False


# ── decision_identifier ───────────────────────────────────────────────────────

async def test_non_threaded_provider_wraps_content_without_llm() -> None:
    groq = AsyncMock()
    with patch.object(decision_identifier, "groq_json", groq):
        moments, usage = await decision_identifier.identify_decisions(
            "full page text", "notion", source_id="pg1", author="Ada", timestamp="t0"
        )
    groq.assert_not_awaited()
    assert usage is None
    assert len(moments) == 1
    assert moments[0].decision_text == "full page text"
    assert moments[0].author == "Ada"


async def test_threaded_provider_parses_decision_moments() -> None:
    payload = {
        "decisions": [
            {"message_id": "m1", "author": "Lead", "timestamp": "t1",
             "decision_text": "Always refund within 30 days.", "signals": ["definitive_language"]},
            # blank decision_text below is dropped by the stage:
            {"message_id": "m2", "author": "x", "timestamp": "t2", "decision_text": "  "},
        ]
    }
    with patch.object(decision_identifier, "groq_json", AsyncMock(return_value=(payload, _USAGE))):
        moments, usage = await decision_identifier.identify_decisions(
            "thread", "slack", source_id="c1", author="", timestamp=""
        )
    assert usage is _USAGE
    assert len(moments) == 1  # blank-text entry dropped
    assert moments[0].signals == ["definitive_language"]


async def test_threaded_provider_empty_decisions() -> None:
    with patch.object(
        decision_identifier, "groq_json", AsyncMock(return_value=({"decisions": []}, _USAGE))
    ):
        moments, _ = await decision_identifier.identify_decisions(
            "thread", "slack", source_id="c1", author="", timestamp=""
        )
    assert moments == []


# ── skill_extractor ───────────────────────────────────────────────────────────

_ANNOT = AuthorityAnnotation(tier="high", weight=1.0)
_DECISIONS = [DecisionMoment("m1", "Lead", "t1", "Refund within 30 days", ["definitive_language"])]


async def test_skill_extractor_builds_draft() -> None:
    payload = {
        "name": "Refund window",
        "trigger": "customer asks for refund",
        "base_logic": "Refund if within 30 days.",
        "exceptions": [{"condition": "vip", "action": "always refund"}],
        "actions": [{"description": "issue refund"}],
        "extraction_confidence": 0.8,
        "uncertainty_notes": "",
    }
    with patch.object(skill_extractor, "sonnet_json", AsyncMock(return_value=(payload, _USAGE))):
        draft, usage = await skill_extractor.extract_skill(_DECISIONS, "ctx", _ANNOT)
    assert draft.name == "Refund window"
    assert draft.extraction_confidence == 0.8
    assert len(draft.exceptions) == 1
    assert usage is _USAGE


async def test_skill_extractor_clamps_confidence() -> None:
    payload = {"trigger": "t", "base_logic": "b", "extraction_confidence": 5.0}
    with patch.object(skill_extractor, "sonnet_json", AsyncMock(return_value=(payload, _USAGE))):
        draft, _ = await skill_extractor.extract_skill(_DECISIONS, "ctx", _ANNOT)
    assert draft.extraction_confidence == 1.0
    assert draft.name == "t"  # falls back to trigger prefix when name missing


async def test_skill_extractor_rejects_empty_skill() -> None:
    payload = {"trigger": "", "base_logic": "", "extraction_confidence": 0.9}
    with (
        patch.object(skill_extractor, "sonnet_json", AsyncMock(return_value=(payload, _USAGE))),
        pytest.raises(ValueError, match="no trigger/base_logic"),
    ):
        await skill_extractor.extract_skill(_DECISIONS, "ctx", _ANNOT)
