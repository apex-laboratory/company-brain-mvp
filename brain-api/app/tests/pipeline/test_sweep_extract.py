"""Unit tests for the sweep_extract job — ordering, sweep-sourced, progress, resume."""
from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

from app.jobs.tasks import sweep_extract as job
from app.pipeline.authority import SweepConfig
from app.pipeline.types import PipelineResult


@asynccontextmanager
async def _fake_session():
    yield MagicMock(commit=AsyncMock())


@asynccontextmanager
async def _null_ctx():
    yield None


def _wire(monkeypatch, queued, run_results, *, cfg=None):
    """Wire the repo + collaborators. ``run_results`` maps event_id → PipelineResult."""
    repo = MagicMock(
        list_sweep_queued_events=AsyncMock(return_value=queued),
        count_sweep_queued_events=AsyncMock(return_value=0),  # nothing left → no continuation
        write_extraction_progress=AsyncMock(),
    )
    ran: list[tuple[str, bool]] = []

    async def fake_run(workspace_id, event_id, *, sweep_sourced):
        ran.append((event_id, sweep_sourced))
        return run_results.get(event_id, PipelineResult(outcome="review", cost_usd=0.01))

    monkeypatch.setattr(job, "get_session", _fake_session)
    monkeypatch.setattr(job, "run_in_tenant", lambda *a, **k: _null_ctx())
    default_cfg = cfg or SweepConfig(rate_per_minute=0, semaphore_limit=5)
    monkeypatch.setattr(job, "_repo", repo)
    monkeypatch.setattr(job, "run_event_safely", fake_run)
    monkeypatch.setattr(job, "sweep_config", lambda: default_cfg)
    monkeypatch.setattr(job, "processing_order", lambda: ["notion", "slack", "zendesk"])
    monkeypatch.setattr(job, "enqueue", AsyncMock())
    return repo, ran


async def test_no_queued_events_is_noop(monkeypatch) -> None:
    repo, ran = _wire(monkeypatch, [], {})
    result = await job.sweep_extract({}, "wrk_1", "swp_1")
    assert result == {"processed": 0}
    assert ran == []
    repo.write_extraction_progress.assert_not_awaited()


async def test_events_run_in_authority_order_and_sweep_sourced(monkeypatch) -> None:
    # Queued arrives slack-then-notion; processing_order puts notion first.
    queued = [("e_slack", "slack"), ("e_notion", "notion"), ("e_zendesk", "zendesk")]
    _repo, ran = _wire(monkeypatch, queued, {})
    await job.sweep_extract({}, "wrk_1", "swp_1")
    assert [eid for eid, _ in ran] == ["e_notion", "e_slack", "e_zendesk"]
    assert all(sweep_sourced for _, sweep_sourced in ran)  # every event sweep-sourced


async def test_tally_counts_outcomes_and_cost(monkeypatch) -> None:
    queued = [("e1", "notion"), ("e2", "notion"), ("e3", "notion"), ("e4", "notion")]
    results = {
        "e1": PipelineResult(outcome="review", cost_usd=0.02),
        "e2": PipelineResult(outcome="duplicate", cost_usd=0.01),
        "e3": PipelineResult(outcome="contradiction", cost_usd=0.03),
        "e4": PipelineResult(outcome="failed", cost_usd=0.0),
    }
    repo, _ = _wire(monkeypatch, queued, results)
    tally = await job.sweep_extract({}, "wrk_1", "swp_1")
    assert tally["processed"] == 4
    assert tally["review"] == 1
    assert tally["duplicates"] == 1
    assert tally["contradictions"] == 1
    assert tally["failed"] == 1
    # Final progress flush carries the cost rollup + zero remaining.
    final = repo.write_extraction_progress.await_args.args[2]
    assert final["queued_remaining"] == 0
    assert final["cost_usd"] == 0.06


async def test_remaining_events_enqueue_a_continuation(monkeypatch) -> None:
    queued = [("e1", "notion"), ("e2", "notion")]
    repo, _ = _wire(monkeypatch, queued, {})
    repo.count_sweep_queued_events = AsyncMock(return_value=5)  # more still queued
    result = await job.sweep_extract({}, "wrk_1", "swp_1")
    assert result["queued_remaining"] == 5
    # A continuation sweep_extract is chained (no stable job id → new run each time).
    job.enqueue.assert_awaited_once_with("sweep_extract", "wrk_1", "swp_1")


async def test_no_remaining_events_does_not_chain(monkeypatch) -> None:
    repo, _ = _wire(monkeypatch, [("e1", "notion")], {})  # count mock returns 0
    await job.sweep_extract({}, "wrk_1", "swp_1")
    job.enqueue.assert_not_awaited()


async def test_semaphore_bounds_concurrency(monkeypatch) -> None:
    import asyncio

    queued = [(f"e{i}", "notion") for i in range(6)]
    live = {"now": 0, "max": 0}

    async def fake_run(workspace_id, event_id, *, sweep_sourced):
        live["now"] += 1
        live["max"] = max(live["max"], live["now"])
        await asyncio.sleep(0)  # yield so overlaps can accumulate
        live["now"] -= 1
        return PipelineResult(outcome="review")

    _repo, _ = _wire(monkeypatch, queued, {}, cfg=SweepConfig(rate_per_minute=0, semaphore_limit=2))
    monkeypatch.setattr(job, "run_event_safely", fake_run)
    await job.sweep_extract({}, "wrk_1", "swp_1")
    assert live["max"] <= 2  # never more than semaphore_limit concurrent runs
