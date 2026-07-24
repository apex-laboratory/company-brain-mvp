"""Unit tests for app/pipeline/stages/skill_writer.py — routing table + write paths."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.pipeline.authority import RoutingConfig
from app.pipeline.repository import SimilarSkill
from app.pipeline.stages.skill_writer import (
    next_version,
    route,
    to_review_confidence,
    write_contradiction,
    write_duplicate,
    write_exception,
    write_new_skill,
    write_update,
)
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


def test_next_version() -> None:
    assert next_version("v1") == "v2"
    assert next_version("v9") == "v10"
    assert next_version("weird") == "v2"


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


async def test_published_path_writes_version() -> None:
    repo = _repo()
    result = await write_new_skill(
        MagicMock(), repo,
        workspace_id="wrk_1", event_id="evt_1", sweep_id=None,
        provider="notion", source_url="http://x", draft=_draft(),
        embedding=[0.0] * 1536, embedding_model="m", confidence=0.95, authority="high",
        sweep_sourced=False, routing=_ROUTING, evidence=None,
    )
    assert result.outcome == "published"
    assert result.skill_id == "skl_1"
    repo.insert_skill.assert_awaited_once()
    repo.insert_skill_version.assert_awaited_once()
    repo.insert_review.assert_not_awaited()
    # Cache invalidation now happens post-commit in the orchestrator, not here.


async def test_review_path_writes_review_row_and_bumps_sweep() -> None:
    repo = _repo()
    evidence = DecisionMoment("m1", "Lead", "t1", "Refund within 30 days")
    result = await write_new_skill(
        MagicMock(), repo,
        workspace_id="wrk_1", event_id="evt_1", sweep_id="swp_1",
        provider="slack", source_url="http://x", draft=_draft(),
        embedding=[0.0] * 1536, embedding_model="m", confidence=0.80, authority="medium",
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
        embedding=[0.0] * 1536, embedding_model="m", confidence=0.5, authority="low",
        sweep_sourced=False, routing=_ROUTING, evidence=None,
    )
    assert result.outcome == "draft"
    repo.insert_review.assert_not_awaited()
    repo.insert_skill_version.assert_not_awaited()


# ── boundary routes: DUPLICATE / UPDATE / EXCEPTION / contradiction ────────────

def _match(version: str = "v1") -> SimilarSkill:
    return SimilarSkill(
        id="skl_x", name="Refund policy", version=version,
        base_logic="refund within 30 days", exceptions_block=[{"condition": "vip"}],
        source_authority="high", similarity=0.95,
    )


async def test_duplicate_appends_source_and_stops() -> None:
    repo = _repo()
    repo.append_source_id = AsyncMock()
    result = await write_duplicate(MagicMock(), repo, matched=_match(), event_id="evt_1")
    assert result.outcome == "duplicate"
    assert result.skill_id == "skl_x"
    append = repo.append_source_id.await_args
    assert append.args[1:] == ("skl_x", "evt_1")
    repo.insert_skill.assert_not_awaited()


async def test_update_published_mutates_skill_and_versions() -> None:
    repo = _repo()
    repo.update_skill_logic = AsyncMock()
    result = await write_update(
        MagicMock(), repo,
        workspace_id="wrk_1", provider="notion", source_url="http://x",
        matched=_match("v1"), draft=_draft(), embedding=[0.0] * 1536, embedding_model="m",
        confidence=0.95, authority="high", sweep_sourced=False, sweep_id=None,
        routing=_ROUTING, evidence=None,
    )
    assert result.outcome == "published"
    # New version row + logic mutation to v2; no review row on the publish branch.
    assert repo.insert_skill_version.await_args.kwargs["version"] == "v2"
    assert repo.insert_skill_version.await_args.kwargs["change_type"] == "update"
    repo.update_skill_logic.assert_awaited_once()
    repo.insert_review.assert_not_awaited()


async def test_update_review_branch_does_not_mutate_skill() -> None:
    repo = _repo()
    repo.update_skill_logic = AsyncMock()
    result = await write_update(
        MagicMock(), repo,
        workspace_id="wrk_1", provider="slack", source_url="http://x",
        matched=_match(), draft=_draft(), embedding=[0.0] * 1536, embedding_model="m",
        confidence=0.80, authority="medium", sweep_sourced=True, sweep_id="swp_1",
        routing=_ROUTING, evidence=None,
    )
    assert result.outcome == "review"
    repo.update_skill_logic.assert_not_awaited()  # applied on approve, not now
    assert repo.insert_review.await_args.kwargs["kind"] == "policy_change"
    assert repo.insert_review.await_args.kwargs["before_text"] == "refund within 30 days"


async def test_exception_published_appends_and_keeps_base_logic() -> None:
    repo = _repo()
    repo.apply_exception = AsyncMock()
    draft = SkillDraft("R", "t", "b", [{"condition": "gov", "action": "waive"}], [], 0.9)
    result = await write_exception(
        MagicMock(), repo,
        workspace_id="wrk_1", provider="notion", source_url="",
        matched=_match(), draft=draft, confidence=0.95, authority="high",
        sweep_sourced=False, sweep_id=None, routing=_ROUTING, evidence=None,
    )
    assert result.outcome == "published"
    # Existing carve-out preserved + the new one appended (base_logic untouched).
    applied = repo.apply_exception.await_args.kwargs["exceptions_block"]
    assert {"condition": "vip"} in applied and {"condition": "gov", "action": "waive"} in applied


async def test_contradiction_opens_two_source_review_without_mutation() -> None:
    repo = _repo()
    source_a = {"url": "a", "author": "Lead", "excerpt": "old", "authority": "high"}
    source_b = {"url": "b", "author": "New", "excerpt": "new", "authority": "medium"}
    result = await write_contradiction(
        MagicMock(), repo,
        workspace_id="wrk_1", provider="slack", matched=_match(),
        draft=_draft(), source_a=source_a, source_b=source_b,
    )
    assert result.outcome == "contradiction"
    kwargs = repo.insert_review.await_args.kwargs
    assert kwargs["kind"] == "contradiction"
    assert kwargs["confidence"] == 0
    assert kwargs["payload"]["source_a"] == source_a
    assert kwargs["payload"]["source_b"] == source_b
    repo.insert_skill.assert_not_awaited()
