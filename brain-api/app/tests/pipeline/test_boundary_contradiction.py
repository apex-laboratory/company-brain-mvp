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
    gemini = AsyncMock()
    with patch.object(boundary_classifier, "llm_json", gemini):
        result, usage = await boundary_classifier.classify_boundary(_draft(), [])
    gemini.assert_not_awaited()
    assert result.classification == "NEW"
    assert result.matched_skill_id is None
    assert usage is None


async def test_similarity_at_or_below_threshold_is_new_without_llm() -> None:
    gemini = AsyncMock()
    with patch.object(boundary_classifier, "llm_json", gemini):
        result, usage = await boundary_classifier.classify_boundary(
            _draft(), [_skill(SIMILARITY_THRESHOLD)]
        )
    gemini.assert_not_awaited()
    assert result.classification == "NEW"
    assert usage is None


# ── boundary: LLM path above threshold ────────────────────────────────────────

async def test_above_threshold_calls_gemini_and_returns_label() -> None:
    with patch.object(
        boundary_classifier, "llm_json",
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
        boundary_classifier, "llm_json",
        AsyncMock(return_value=({"classification": "???"}, _USAGE)),
    ):
        result, _ = await boundary_classifier.classify_boundary(_draft(), [_skill(0.95)])
    assert result.classification == "NEW"
    assert result.matched_skill_id == "skl_1"  # match still recorded


# ── contradiction detector ────────────────────────────────────────────────────

async def test_contradiction_true() -> None:
    with patch.object(
        contradiction_detector, "llm_json",
        AsyncMock(return_value=({"has_contradiction": True, "reason": "conflict"}, _USAGE)),
    ):
        has, usage = await contradiction_detector.detect_contradiction(_draft(), _skill(0.9))
    assert has is True
    assert usage is _USAGE


async def test_contradiction_false() -> None:
    with patch.object(
        contradiction_detector, "llm_json",
        AsyncMock(return_value=({"has_contradiction": False}, _USAGE)),
    ):
        has, _ = await contradiction_detector.detect_contradiction(_draft(), _skill(0.9))
    assert has is False


# ── contradiction screen ──────────────────────────────────────────────────────
# The screen runs on its own threshold, well below the boundary gate: a rule and
# its negation are about one topic but assert opposite things, which pushes their
# vectors apart. Anything gated on SIMILARITY_THRESHOLD misses them entirely.


def _skills(*sims: float) -> list[SimilarSkill]:
    """Candidates as similar_skills returns them — descending similarity."""
    return [_skill(s, name=f"skill-{i}") for i, s in enumerate(sims)]


async def test_screen_finds_conflict_below_boundary_threshold() -> None:
    """0.751 — the real production near-miss — is screened, though it would never
    have reached the boundary classifier."""
    candidates = _skills(0.751)
    assert candidates[0].similarity < SIMILARITY_THRESHOLD
    with patch.object(
        contradiction_detector, "llm_json",
        AsyncMock(return_value=({"has_contradiction": True}, _USAGE)),
    ):
        conflict, usages = await contradiction_detector.screen(_draft(), candidates)
    assert conflict is candidates[0]
    assert len(usages) == 1


async def test_screen_stops_at_first_out_of_band_candidate() -> None:
    """Candidates arrive ordered by distance, so the first one below the threshold
    ends the scan — no LLM call is spent on anything further away."""
    gemini = AsyncMock(return_value=({"has_contradiction": False}, _USAGE))
    with patch.object(contradiction_detector, "llm_json", gemini):
        conflict, usages = await contradiction_detector.screen(
            _draft(), _skills(0.70, 0.431, 0.30)
        )
    assert conflict is None
    assert gemini.await_count == 1  # only the 0.70 candidate was in band
    assert len(usages) == 1


async def test_screen_short_circuits_on_first_conflict() -> None:
    gemini = AsyncMock(
        side_effect=[
            ({"has_contradiction": False}, _USAGE),
            ({"has_contradiction": True}, _USAGE),
        ]
    )
    candidates = _skills(0.80, 0.72, 0.65)
    with patch.object(contradiction_detector, "llm_json", gemini):
        conflict, usages = await contradiction_detector.screen(_draft(), candidates)
    assert conflict is candidates[1]
    assert gemini.await_count == 2  # the 0.65 candidate is never reached
    assert len(usages) == 2


async def test_screen_with_no_candidates_costs_nothing() -> None:
    gemini = AsyncMock()
    with patch.object(contradiction_detector, "llm_json", gemini):
        conflict, usages = await contradiction_detector.screen(_draft(), [])
    assert conflict is None and usages == []
    gemini.assert_not_awaited()
