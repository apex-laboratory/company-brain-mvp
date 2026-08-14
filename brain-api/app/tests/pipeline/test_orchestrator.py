"""Orchestrator happy-path + early-exit tests.

Every stage is mocked; this exercises the sequencing, discard short-circuits, and
the final write/finalize wiring — no DB, LLM, or network.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.integrations.base import RawEvent
from app.pipeline import orchestrator
from app.pipeline.authority import AuthorityAnnotation, RoutingConfig
from app.pipeline.repository import EventRow, SimilarSkill
from app.pipeline.types import DecisionMoment, PipelineResult, SkillDraft, StageUsage

_USAGE = StageUsage(stage="s", model="m", input_tokens=1, output_tokens=1, cost_usd=0.001)


def _event(processed: bool = False, sweep_id: str | None = None) -> EventRow:
    return EventRow(
        id="evt_1", workspace_id="wrk_1", provider="slack", event_type="message",
        source_id="c1", external_event_id="c1:1", source_connection_id="src_1",
        payload={"channel": "policy"},
        processed=processed, sweep_id=sweep_id, attempts=0,
        created_at=datetime(2026, 7, 1, tzinfo=UTC),
    )


def _raw(content: str = "We always refund within 30 days.") -> RawEvent:
    return RawEvent(
        provider="slack", source_id="c1", external_event_id="c1:1", event_type="message",
        actor={"id": "u1", "name": "Lead"}, content=content,
        created_at=datetime(2026, 7, 1, tzinfo=UTC), url="http://slack/x", raw={},
    )


@asynccontextmanager
async def _fake_session():
    yield MagicMock(commit=AsyncMock())


@asynccontextmanager
async def _null_ctx():
    yield None


@pytest.fixture
def wired(monkeypatch):
    """Wire the DB/session boundary + repository; return the repo mock."""
    repo = MagicMock(
        load_event=AsyncMock(return_value=_event()),
        finalize_event=AsyncMock(),
        similar_skills=AsyncMock(return_value=[]),  # default: no neighbours → NEW
    )
    monkeypatch.setattr(orchestrator, "get_tenant_session", _fake_session)
    monkeypatch.setattr(orchestrator, "run_in_tenant", lambda *a, **k: _null_ctx())
    monkeypatch.setattr(orchestrator, "_repo", repo)
    monkeypatch.setattr(orchestrator, "_normalize", lambda event: _raw())
    # Context expansion is exercised in test_expanders; here it's a passthrough
    # so the orchestrator tests stay off the DB/network.
    monkeypatch.setattr(
        orchestrator, "_expand", AsyncMock(side_effect=lambda ev, raw: (raw.content, None))
    )
    monkeypatch.setattr(
        orchestrator.authority_mod, "annotate",
        lambda p, pl: AuthorityAnnotation(tier="medium", weight=0.7),
    )
    monkeypatch.setattr(orchestrator.authority_mod, "routing_config", lambda: RoutingConfig())
    return repo


async def _mock_stages(monkeypatch, *, relevant=True, decisions=None, draft=True):
    monkeypatch.setattr(
        orchestrator.relevance_gate, "is_relevant",
        AsyncMock(return_value=(relevant, "reason", _USAGE)),
    )
    moments = decisions if decisions is not None else [
        DecisionMoment("m1", "Lead", "t1", "Refund within 30 days", ["definitive_language"])
    ]
    monkeypatch.setattr(
        orchestrator.decision_identifier, "identify_decisions",
        AsyncMock(return_value=(moments, _USAGE)),
    )
    if draft:
        d = SkillDraft("Refund", "refund asked", "refund <30d", [], [], 0.9)
        monkeypatch.setattr(
            orchestrator.skill_extractor, "extract_skill",
            AsyncMock(return_value=(d, _USAGE)),
        )
    monkeypatch.setattr(
        orchestrator.embedder, "embed_text",
        AsyncMock(return_value=([0.0] * 1536, _USAGE)),
    )


async def test_happy_path_writes_skill_and_finalizes(wired, monkeypatch) -> None:
    await _mock_stages(monkeypatch)
    writer = AsyncMock(
        return_value=PipelineResult(outcome="review", skill_id="skl_1", review_id="rev_1")
    )
    monkeypatch.setattr(orchestrator.skill_writer, "write_new_skill", writer)

    result = await orchestrator.run_pipeline("wrk_1", "evt_1")

    assert result.outcome == "review"
    writer.assert_awaited_once()
    # Event finalized with the writer's outcome + a cost meta blob.
    final = wired.finalize_event.await_args
    assert final.kwargs["outcome"] == "review"
    assert final.kwargs["pipeline_meta"]["costs"]["total_usd"] > 0


async def test_published_outcome_invalidates_cache_after_commit(wired, monkeypatch) -> None:
    await _mock_stages(monkeypatch)
    writer = AsyncMock(return_value=PipelineResult(outcome="published", skill_id="skl_1"))
    monkeypatch.setattr(orchestrator.skill_writer, "write_new_skill", writer)
    inval = AsyncMock()
    monkeypatch.setattr(orchestrator.cache, "invalidate_skills", inval)

    result = await orchestrator.run_pipeline("wrk_1", "evt_1")

    assert result.outcome == "published"
    # Invalidation runs post-commit (finalize happened first) and only on publish.
    wired.finalize_event.assert_awaited_once()
    inval.assert_awaited_once_with("wrk_1")


async def test_review_outcome_does_not_invalidate_cache(wired, monkeypatch) -> None:
    await _mock_stages(monkeypatch)
    writer = AsyncMock(return_value=PipelineResult(outcome="review", skill_id="skl_1"))
    monkeypatch.setattr(orchestrator.skill_writer, "write_new_skill", writer)
    inval = AsyncMock()
    monkeypatch.setattr(orchestrator.cache, "invalidate_skills", inval)

    await orchestrator.run_pipeline("wrk_1", "evt_1")

    inval.assert_not_awaited()


async def test_irrelevant_content_discarded_before_extraction(wired, monkeypatch) -> None:
    await _mock_stages(monkeypatch, relevant=False)
    writer = AsyncMock()
    monkeypatch.setattr(orchestrator.skill_writer, "write_new_skill", writer)

    result = await orchestrator.run_pipeline("wrk_1", "evt_1")

    assert result.outcome == "discarded"
    writer.assert_not_awaited()
    assert wired.finalize_event.await_args.kwargs["outcome"] == "discarded"


async def test_no_decisions_discarded(wired, monkeypatch) -> None:
    await _mock_stages(monkeypatch, decisions=[])
    result = await orchestrator.run_pipeline("wrk_1", "evt_1")
    assert result.outcome == "discarded"


async def test_empty_content_discarded_without_gate(wired, monkeypatch) -> None:
    monkeypatch.setattr(orchestrator, "_normalize", lambda event: _raw(content="   "))
    gate = AsyncMock()
    monkeypatch.setattr(orchestrator.relevance_gate, "is_relevant", gate)
    result = await orchestrator.run_pipeline("wrk_1", "evt_1")
    assert result.outcome == "discarded"
    gate.assert_not_awaited()


async def test_threaded_provider_gates_on_expanded_thread(wired, monkeypatch) -> None:
    """On a threaded provider the gate must judge the whole thread, not the parent.

    A Q&A thread's parent is the question ("ok to skip the auth test?"); the policy
    lives in a reply. Gating the parent alone discarded the thread before its answer
    was ever fetched, so every Slack Q&A thread was dropped at the gate.
    """
    monkeypatch.setattr(orchestrator, "_normalize", lambda event: _raw(content="Ok to skip?"))
    monkeypatch.setattr(
        orchestrator, "_expand",
        AsyncMock(return_value=("Ok to skip?\nLead: No — auth tests are required.", None)),
    )
    await _mock_stages(monkeypatch)
    monkeypatch.setattr(
        orchestrator.skill_writer, "write_new_skill",
        AsyncMock(return_value=PipelineResult(outcome="review", skill_id="skl_1")),
    )

    await orchestrator.run_pipeline("wrk_1", "evt_1")

    gated_text = orchestrator.relevance_gate.is_relevant.await_args.args[0]
    assert "auth tests are required" in gated_text


async def test_non_threaded_provider_skips_expansion_when_irrelevant(
    wired, monkeypatch
) -> None:
    """Single-document providers still expand *after* the gate, so an irrelevant
    page never costs a fetch."""
    monkeypatch.setattr(
        orchestrator, "_normalize",
        lambda event: RawEvent(
            provider="notion", source_id="p1", external_event_id="p1", event_type="page",
            actor={"id": "u1"}, content="lunch menu",
            created_at=datetime(2026, 7, 1, tzinfo=UTC), url="http://notion/x", raw={},
        ),
    )
    wired.load_event = AsyncMock(
        return_value=EventRow(
            id="evt_1", workspace_id="wrk_1", provider="notion", event_type="page",
            source_id="p1", external_event_id="p1", source_connection_id="src_1",
            payload={}, processed=False, sweep_id=None, attempts=0,
            created_at=datetime(2026, 7, 1, tzinfo=UTC),
        )
    )
    expand = AsyncMock(return_value=("expanded", None))
    monkeypatch.setattr(orchestrator, "_expand", expand)
    await _mock_stages(monkeypatch, relevant=False)

    result = await orchestrator.run_pipeline("wrk_1", "evt_1")

    assert result.outcome == "discarded"
    expand.assert_not_awaited()


async def test_threaded_expander_failure_recorded_on_gate_discard(
    wired, monkeypatch
) -> None:
    """Expansion runs first on threaded providers, so a failure there must still be
    recorded when the gate then discards on the fallback content."""
    monkeypatch.setattr(
        orchestrator, "_expand",
        AsyncMock(return_value=("Ok to skip?", "HTTPStatusError: 500")),
    )
    await _mock_stages(monkeypatch, relevant=False)

    result = await orchestrator.run_pipeline("wrk_1", "evt_1")

    assert result.outcome == "discarded"
    meta = wired.finalize_event.await_args.kwargs["pipeline_meta"]
    assert meta["expander_error"] == "HTTPStatusError: 500"
    assert meta["stage"] == "relevance_gate"


async def test_already_processed_event_skipped(wired, monkeypatch) -> None:
    wired.load_event = AsyncMock(return_value=_event(processed=True))
    monkeypatch.setattr(orchestrator, "_repo", wired)
    result = await orchestrator.run_pipeline("wrk_1", "evt_1")
    assert result.outcome == "skipped"


async def test_missing_event_returns_failed(wired, monkeypatch) -> None:
    wired.load_event = AsyncMock(return_value=None)
    monkeypatch.setattr(orchestrator, "_repo", wired)
    result = await orchestrator.run_pipeline("wrk_1", "evt_1")
    assert result.outcome == "failed"


# ── boundary routing ──────────────────────────────────────────────────────────

def _similar() -> SimilarSkill:
    return SimilarSkill(
        id="skl_x", name="Refund", version="v1", base_logic="refund <30d",
        exceptions_block=[], source_authority="high", similarity=0.95,
    )


async def test_update_with_contradiction_routes_to_contradiction(wired, monkeypatch) -> None:
    await _mock_stages(monkeypatch)
    wired.similar_skills = AsyncMock(return_value=[_similar()])
    wired.skill_provenance = AsyncMock(return_value=None)  # → fallback source_a
    monkeypatch.setattr(orchestrator, "_repo", wired)
    monkeypatch.setattr(
        orchestrator.boundary_classifier, "classify_boundary",
        AsyncMock(return_value=(
            orchestrator.boundary_classifier.BoundaryResult("UPDATE", "skl_x", 0.95), _USAGE,
        )),
    )
    monkeypatch.setattr(
        orchestrator.contradiction_detector, "detect_contradiction",
        AsyncMock(return_value=(True, _USAGE)),
    )
    contra = AsyncMock(return_value=PipelineResult(outcome="contradiction", skill_id="skl_x"))
    monkeypatch.setattr(orchestrator.skill_writer, "write_contradiction", contra)

    result = await orchestrator.run_pipeline("wrk_1", "evt_1")

    assert result.outcome == "contradiction"
    contra.assert_awaited_once()
    assert wired.finalize_event.await_args.kwargs["outcome"] == "contradiction"


async def test_update_without_contradiction_routes_to_update(wired, monkeypatch) -> None:
    await _mock_stages(monkeypatch)
    wired.similar_skills = AsyncMock(return_value=[_similar()])
    monkeypatch.setattr(orchestrator, "_repo", wired)
    monkeypatch.setattr(
        orchestrator.boundary_classifier, "classify_boundary",
        AsyncMock(return_value=(
            orchestrator.boundary_classifier.BoundaryResult("UPDATE", "skl_x", 0.95), _USAGE,
        )),
    )
    monkeypatch.setattr(
        orchestrator.contradiction_detector, "detect_contradiction",
        AsyncMock(return_value=(False, _USAGE)),
    )
    upd = AsyncMock(return_value=PipelineResult(outcome="review", skill_id="skl_x"))
    monkeypatch.setattr(orchestrator.skill_writer, "write_update", upd)

    result = await orchestrator.run_pipeline("wrk_1", "evt_1")

    assert result.outcome == "review"
    upd.assert_awaited_once()


async def test_expander_error_is_recorded_not_fatal(wired, monkeypatch) -> None:
    # A failing expander must not stop the pipeline: it falls back to raw content
    # and records the error in pipeline_meta.
    await _mock_stages(monkeypatch)
    monkeypatch.setattr(
        orchestrator, "_expand",
        AsyncMock(side_effect=lambda ev, raw: (raw.content, "HTTPStatusError: 500")),
    )
    writer = AsyncMock(return_value=PipelineResult(outcome="review", skill_id="skl_1"))
    monkeypatch.setattr(orchestrator.skill_writer, "write_new_skill", writer)

    result = await orchestrator.run_pipeline("wrk_1", "evt_1")

    assert result.outcome == "review"  # pipeline still completed
    meta = wired.finalize_event.await_args.kwargs["pipeline_meta"]
    assert meta["expander_error"] == "HTTPStatusError: 500"


async def test_duplicate_short_circuits_without_contradiction(wired, monkeypatch) -> None:
    await _mock_stages(monkeypatch)
    wired.similar_skills = AsyncMock(return_value=[_similar()])
    monkeypatch.setattr(orchestrator, "_repo", wired)
    monkeypatch.setattr(
        orchestrator.boundary_classifier, "classify_boundary",
        AsyncMock(return_value=(
            orchestrator.boundary_classifier.BoundaryResult("DUPLICATE", "skl_x", 0.99), _USAGE,
        )),
    )
    contra = AsyncMock()
    monkeypatch.setattr(orchestrator.contradiction_detector, "detect_contradiction", contra)
    dup = AsyncMock(return_value=PipelineResult(outcome="duplicate", skill_id="skl_x"))
    monkeypatch.setattr(orchestrator.skill_writer, "write_duplicate", dup)

    result = await orchestrator.run_pipeline("wrk_1", "evt_1")

    assert result.outcome == "duplicate"
    contra.assert_not_awaited()  # duplicates never reach contradiction detection
    dup.assert_awaited_once()


async def test_new_boundary_still_screened_for_contradiction(wired, monkeypatch) -> None:
    """The regression: a draft below SIMILARITY_THRESHOLD is NEW to the boundary
    classifier, which never even ran — but it can still assert the opposite of a
    published rule. Two such pairs went live at 0.751 and 0.648 with empty
    conflict_flags. Screening must be independent of the boundary label.
    """
    await _mock_stages(monkeypatch)
    near = SimilarSkill(
        id="skl_x", name="Exception handling", version="v1",
        base_logic="never swallow exceptions — always log with context",
        exceptions_block=[], source_authority="high", similarity=0.751,
    )
    wired.similar_skills = AsyncMock(return_value=[near])
    wired.skill_provenance = AsyncMock(return_value=None)
    monkeypatch.setattr(orchestrator, "_repo", wired)
    monkeypatch.setattr(
        orchestrator.boundary_classifier, "classify_boundary",
        AsyncMock(return_value=(
            orchestrator.boundary_classifier.BoundaryResult("NEW", None, 0.751), None,
        )),
    )
    monkeypatch.setattr(
        orchestrator.contradiction_detector, "detect_contradiction",
        AsyncMock(return_value=(True, _USAGE)),
    )
    contra = AsyncMock(return_value=PipelineResult(outcome="contradiction", skill_id="skl_x"))
    monkeypatch.setattr(orchestrator.skill_writer, "write_contradiction", contra)
    new_skill = AsyncMock()
    monkeypatch.setattr(orchestrator.skill_writer, "write_new_skill", new_skill)

    result = await orchestrator.run_pipeline("wrk_1", "evt_1")

    assert result.outcome == "contradiction"
    # The conflicting skill is the review's subject even though `matched` is None
    # (the boundary classifier claimed no match).
    assert contra.await_args.kwargs["matched"] is near
    new_skill.assert_not_awaited()  # a competing rule must never publish alongside


async def test_distant_neighbour_is_never_screened(wired, monkeypatch) -> None:
    """Below CONTRADICTION_THRESHOLD nothing is close enough to conflict, so the
    draft writes as NEW without spending an LLM call."""
    await _mock_stages(monkeypatch)
    far = SimilarSkill(
        id="skl_y", name="Unrelated", version="v1", base_logic="something else",
        exceptions_block=[], source_authority="low", similarity=0.431,
    )
    wired.similar_skills = AsyncMock(return_value=[far])
    monkeypatch.setattr(orchestrator, "_repo", wired)
    monkeypatch.setattr(
        orchestrator.boundary_classifier, "classify_boundary",
        AsyncMock(return_value=(
            orchestrator.boundary_classifier.BoundaryResult("NEW", None, 0.431), None,
        )),
    )
    contra = AsyncMock()
    monkeypatch.setattr(orchestrator.contradiction_detector, "detect_contradiction", contra)
    writer = AsyncMock(return_value=PipelineResult(outcome="review", skill_id="skl_1"))
    monkeypatch.setattr(orchestrator.skill_writer, "write_new_skill", writer)

    result = await orchestrator.run_pipeline("wrk_1", "evt_1")

    assert result.outcome == "review"
    contra.assert_not_awaited()
    writer.assert_awaited_once()
