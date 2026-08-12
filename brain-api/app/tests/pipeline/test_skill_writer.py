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

_ROUTING = RoutingConfig(review_queue_confidence_floor=0.70)


# ── route() truth table ───────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("confidence", "expected"),
    [
        (1.00, "review"),   # even a perfect score only earns a reviewer's attention
        (0.95, "review"),
        (0.70, "review"),   # at review floor
        (0.69, "draft"),    # below review floor
        (0.10, "draft"),
    ],
)
def test_route_table(confidence, expected) -> None:
    assert route(confidence, _ROUTING) == expected


def test_route_never_publishes_at_any_confidence() -> None:
    """The registry is reachable only through human approval — no confidence,
    authority tier, or provenance may bypass the queue."""
    assert all(
        route(c / 100, _ROUTING) != "published" for c in range(0, 101)
    )


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


async def test_max_confidence_high_authority_still_queues_for_review() -> None:
    """The old auto-publish case: top confidence from a high-authority source. It
    must now land in the queue as ``review`` with no version row — the version is
    written when a human approves."""
    repo = _repo()
    result = await write_new_skill(
        MagicMock(), repo,
        workspace_id="wrk_1", event_id="evt_1", sweep_id=None,
        provider="notion", source_url="http://x", draft=_draft(),
        embedding=[0.0] * 1536, embedding_model="m", confidence=1.0, authority="high",
        routing=_ROUTING, evidence=None,
    )
    assert result.outcome == "review"
    assert result.skill_id == "skl_1"
    repo.insert_skill.assert_awaited_once()
    repo.insert_skill_version.assert_not_awaited()
    repo.insert_review.assert_awaited_once()
    # The skill row itself must not be born active.
    assert repo.insert_skill.await_args.kwargs["status"] == "review"


async def test_review_path_writes_review_row_and_bumps_sweep() -> None:
    repo = _repo()
    evidence = DecisionMoment("m1", "Lead", "t1", "Refund within 30 days")
    result = await write_new_skill(
        MagicMock(), repo,
        workspace_id="wrk_1", event_id="evt_1", sweep_id="swp_1",
        provider="slack", source_url="http://x", draft=_draft(),
        embedding=[0.0] * 1536, embedding_model="m", confidence=0.80, authority="medium",
        routing=_ROUTING, evidence=evidence,
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
        routing=_ROUTING, evidence=None,
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


async def test_update_never_mutates_live_skill_even_at_max_confidence() -> None:
    """A live skill agents already act on must never be rewritten without a human,
    however confident the extraction or authoritative the source."""
    repo = _repo()
    repo.update_skill_logic = AsyncMock()
    result = await write_update(
        MagicMock(), repo,
        workspace_id="wrk_1", provider="notion", source_url="http://x",
        matched=_match("v1"), draft=_draft(), confidence=1.0, sweep_id=None,
        routing=_ROUTING, evidence=None,
    )
    assert result.outcome == "review"
    repo.update_skill_logic.assert_not_awaited()
    repo.insert_skill_version.assert_not_awaited()
    assert repo.insert_review.await_args.kwargs["kind"] == "policy_change"


async def test_update_review_branch_does_not_mutate_skill() -> None:
    repo = _repo()
    repo.update_skill_logic = AsyncMock()
    result = await write_update(
        MagicMock(), repo,
        workspace_id="wrk_1", provider="slack", source_url="http://x",
        matched=_match(), draft=_draft(), confidence=0.80, sweep_id="swp_1",
        routing=_ROUTING, evidence=None,
    )
    assert result.outcome == "review"
    repo.update_skill_logic.assert_not_awaited()  # applied on approve, not now
    assert repo.insert_review.await_args.kwargs["kind"] == "policy_change"
    assert repo.insert_review.await_args.kwargs["before_text"] == "refund within 30 days"


async def test_exception_never_applies_carve_out_without_approval() -> None:
    """A carve-out narrows a live rule, so it needs the same human gate as any
    other change — queued as an ``exception`` review, applied on approve."""
    repo = _repo()
    repo.apply_exception = AsyncMock()
    draft = SkillDraft("R", "t", "b", [{"condition": "gov", "action": "waive"}], [], 0.9)
    result = await write_exception(
        MagicMock(), repo,
        workspace_id="wrk_1", provider="notion", source_url="",
        matched=_match(), draft=draft, confidence=1.0, sweep_id=None,
        routing=_ROUTING, evidence=None,
    )
    assert result.outcome == "review"
    repo.apply_exception.assert_not_awaited()
    repo.insert_skill_version.assert_not_awaited()
    assert repo.insert_review.await_args.kwargs["kind"] == "exception"


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
