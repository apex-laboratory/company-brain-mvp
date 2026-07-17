"""Unit tests for the boundary classifier + contradiction detector stages."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from app.pipeline.repository import SimilarSkill
from app.pipeline.stages import boundary_classifier, contradiction_detector
from app.pipeline.types import SIMILARITY_THRESHOLD, SkillDraft, StageUsage

_USAGE = StageUsage(stage="s", model="m", input_tokens=1, output_tokens=1, cost_usd=0.0)


def _draft() -> SkillDraft:
    return SkillDraft("Refund", "refund asked", "refund within 30 days", [], [], 0.9)


def _skill(sim: float, name: str = "Refund policy") -> SimilarSkill:
    return SimilarSkill(
        id="skl_1", name=name, version="v1", base_logic="refund within 30 days",
        exceptions_block=[], source_authority="high", similarity=sim,
    )


# ── boundary: no-LLM NEW below threshold ──────────────────────────────────────

async def test_no_similar_skills_is_new_without_llm() -> None:
    groq = AsyncMock()
    with patch.object(boundary_classifier, "groq_json", groq):
        result, usage = await boundary_classifier.classify_boundary(_draft(), [])
    groq.assert_not_awaited()
    assert result.classification == "NEW"
    assert result.matched_skill_id is None
    assert usage is None


async def test_similarity_at_or_below_threshold_is_new_without_llm() -> None:
    groq = AsyncMock()
    with patch.object(boundary_classifier, "groq_json", groq):
        result, usage = await boundary_classifier.classify_boundary(
            _draft(), [_skill(SIMILARITY_THRESHOLD)]
        )
    groq.assert_not_awaited()
    assert result.classification == "NEW"
    assert usage is None


# ── boundary: LLM path above threshold ────────────────────────────────────────

async def test_above_threshold_calls_groq_and_returns_label() -> None:
    with patch.object(
        boundary_classifier, "groq_json",
        AsyncMock(return_value=({"classification": "UPDATE", "reason": "newer"}, _USAGE)),
    ):
        result, usage = await boundary_classifier.classify_boundary(
            _draft(), [_skill(0.91), _skill(0.80)]
        )
    assert result.classification == "UPDATE"
    assert result.matched_skill_id == "skl_1"
    assert result.similarity == 0.91
    assert usage is _USAGE


async def test_unparseable_label_falls_back_to_new() -> None:
    with patch.object(
        boundary_classifier, "groq_json",
        AsyncMock(return_value=({"classification": "???"}, _USAGE)),
    ):
        result, _ = await boundary_classifier.classify_boundary(_draft(), [_skill(0.95)])
    assert result.classification == "NEW"
    assert result.matched_skill_id == "skl_1"  # match still recorded


# ── contradiction detector ────────────────────────────────────────────────────

async def test_contradiction_true() -> None:
    with patch.object(
        contradiction_detector, "sonnet_json",
        AsyncMock(return_value=({"has_contradiction": True, "reason": "conflict"}, _USAGE)),
    ):
        has, usage = await contradiction_detector.detect_contradiction(_draft(), _skill(0.9))
    assert has is True
    assert usage is _USAGE


async def test_contradiction_false() -> None:
    with patch.object(
        contradiction_detector, "sonnet_json",
        AsyncMock(return_value=({"has_contradiction": False}, _USAGE)),
    ):
        has, _ = await contradiction_detector.detect_contradiction(_draft(), _skill(0.9))
    assert has is False
