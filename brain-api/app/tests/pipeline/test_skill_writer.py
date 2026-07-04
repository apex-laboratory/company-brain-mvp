"""Unit tests for app/pipeline/stages/skill_writer.py — routing table + write paths."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.pipeline.authority import RoutingConfig
from app.pipeline.stages import skill_writer
from app.pipeline.stages.skill_writer import route, to_review_confidence, write_new_skill
from app.pipeline.types import DecisionMoment, SkillDraft

_ROUTING = RoutingConfig(
    auto_publish_confidence=0.90,
    auto_publish_authority_floor="medium",
    review_queue_confidence_floor=0.70,
)


# ── route() truth table ───────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("confidence", "authority", "sweep", "expected"),
    [
        (0.95, "high", False, "published"),
        (0.95, "medium", False, "published"),
        (0.95, "low", False, "review"),      # below authority floor
        (0.95, "high", True, "review"),       # sweep never auto-publishes
        (0.85, "high", False, "review"),      # below publish threshold
        (0.70, "high", False, "review"),      # at review floor
        (0.69, "high", False, "draft"),       # below review floor
        (0.10, "low", False, "draft"),
    ],
)
def test_route_table(confidence, authority, sweep, expected) -> None:
    assert route(confidence, authority, sweep, _ROUTING) == expected


def test_to_review_confidence_scales_and_clamps() -> None:
    assert to_review_confidence(0.765) == 76
    assert to_review_confidence(1.5) == 100
    assert to_review_confidence(-1.0) == 0


# ── write paths ───────────────────────────────────────────────────────────────

def _draft() -> SkillDraft:
    return SkillDraft(
        name="Refund window",
        trigger="customer asks for refund",
        base_logic="Refund if within 30 days.",
        exceptions=[],
        actions=[],
        extraction_confidence=0.9,
    )


def _repo() -> MagicMock:
    return MagicMock(
        resolve_skill_name=AsyncMock(side_effect=lambda s, ws, n: n),
        insert_skill=AsyncMock(return_value="skl_1"),
        insert_skill_version=AsyncMock(return_value="skv_1"),
        insert_review=AsyncMock(return_value="rev_1"),
        bump_sweep_counters=AsyncMock(),
    )


async def test_published_path_writes_version_and_invalidates() -> None:
    repo = _repo()
    with patch.object(skill_writer.cache, "invalidate_skills", AsyncMock()) as inval:
        result = await write_new_skill(
            MagicMock(), repo,
            workspace_id="wrk_1", event_id="evt_1", sweep_id=None,
            provider="notion", source_url="http://x", draft=_draft(),
            embedding=[0.0] * 1536, confidence=0.95, authority="high",
            sweep_sourced=False, routing=_ROUTING, evidence=None,
        )
    assert result.outcome == "published"
    assert result.skill_id == "skl_1"
    repo.insert_skill.assert_awaited_once()
    repo.insert_skill_version.assert_awaited_once()
    repo.insert_review.assert_not_awaited()
    inval.assert_awaited_once_with("wrk_1")


async def test_review_path_writes_review_row_and_bumps_sweep() -> None:
    repo = _repo()
    evidence = DecisionMoment("m1", "Lead", "t1", "Refund within 30 days")
    with patch.object(skill_writer.cache, "invalidate_skills", AsyncMock()):
        result = await write_new_skill(
            MagicMock(), repo,
            workspace_id="wrk_1", event_id="evt_1", sweep_id="swp_1",
            provider="slack", source_url="http://x", draft=_draft(),
            embedding=[0.0] * 1536, confidence=0.80, authority="medium",
            sweep_sourced=True, routing=_ROUTING, evidence=evidence,
        )
    assert result.outcome == "review"
    assert result.review_id == "rev_1"
    repo.insert_skill_version.assert_not_awaited()  # version row created on approve
    kwargs = repo.insert_review.await_args.kwargs
    assert kwargs["kind"] == "new_decision"
    assert kwargs["confidence"] == 80
    assert kwargs["evidence_author"] == "Lead"
    assert kwargs["payload"]["boundary"] == "NEW"
    bump = repo.bump_sweep_counters.await_args
    assert bump.args[1] == "swp_1" and bump.kwargs == {"queued": 1}


async def test_draft_path_writes_only_skill() -> None:
    repo = _repo()
    result = await write_new_skill(
        MagicMock(), repo,
        workspace_id="wrk_1", event_id="evt_1", sweep_id=None,
        provider="slack", source_url="", draft=_draft(),
        embedding=[0.0] * 1536, confidence=0.5, authority="low",
        sweep_sourced=False, routing=_ROUTING, evidence=None,
    )
    assert result.outcome == "draft"
    repo.insert_review.assert_not_awaited()
    repo.insert_skill_version.assert_not_awaited()
