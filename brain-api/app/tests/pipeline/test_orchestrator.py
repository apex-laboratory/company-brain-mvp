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
from app.pipeline.repository import EventRow
from app.pipeline.types import DecisionMoment, PipelineResult, SkillDraft, StageUsage

_USAGE = StageUsage(stage="s", model="m", input_tokens=1, output_tokens=1, cost_usd=0.001)


def _event(processed: bool = False, sweep_id: str | None = None) -> EventRow:
    return EventRow(
        id="evt_1", workspace_id="wrk_1", provider="slack", event_type="message",
        source_id="c1", external_event_id="c1:1", payload={"channel": "policy"},
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
    )
    monkeypatch.setattr(orchestrator, "get_session", _fake_session)
    monkeypatch.setattr(orchestrator, "run_in_tenant", lambda *a, **k: _null_ctx())
    monkeypatch.setattr(orchestrator, "_repo", repo)
    monkeypatch.setattr(orchestrator, "_normalize", lambda event: _raw())
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
