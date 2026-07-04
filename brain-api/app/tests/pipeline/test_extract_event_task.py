"""Unit tests for the extract_event ARQ task — dead-letter contract."""
from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

from app.jobs.tasks import extract_event as task
from app.pipeline.llm.retry import LLMExhaustedError
from app.pipeline.types import PipelineResult


@asynccontextmanager
async def _fake_session():
    yield MagicMock(commit=AsyncMock())


def _wire_finalize(monkeypatch) -> MagicMock:
    """Wire get_session/run_in_tenant so the failure-finalize path is inert."""
    repo = MagicMock(finalize_event=AsyncMock())
    monkeypatch.setattr(task, "get_session", _fake_session)
    monkeypatch.setattr(task, "run_in_tenant", lambda *a, **k: _null_ctx())
    monkeypatch.setattr(task, "_repo", repo)
    return repo


@asynccontextmanager
async def _null_ctx():
    yield None


async def test_success_returns_outcome(monkeypatch) -> None:
    monkeypatch.setattr(
        task, "run_pipeline",
        AsyncMock(return_value=PipelineResult(outcome="published", skill_id="skl_1")),
    )
    result = await task.extract_event({}, "wrk_1", "evt_1")
    assert result == {"outcome": "published", "skill_id": "skl_1"}


async def test_sweep_id_marks_sweep_sourced(monkeypatch) -> None:
    run = AsyncMock(return_value=PipelineResult(outcome="review"))
    monkeypatch.setattr(task, "run_pipeline", run)
    await task.extract_event({}, "wrk_1", "evt_1", sweep_id="swp_1")
    assert run.await_args.kwargs["sweep_sourced"] is True


async def test_exhausted_llm_dead_letters_without_raising(monkeypatch) -> None:
    repo = _wire_finalize(monkeypatch)
    monkeypatch.setattr(
        task, "run_pipeline", AsyncMock(side_effect=LLMExhaustedError("gate failed"))
    )
    result = await task.extract_event({}, "wrk_1", "evt_1")
    assert result == {"outcome": "failed"}
    # The event is marked failed (dead-letter), not left silently unprocessed.
    finalize = repo.finalize_event.await_args
    assert finalize.kwargs["outcome"] == "failed"
    assert "LLMExhaustedError" in finalize.kwargs["pipeline_meta"]["error"]


async def test_unexpected_error_also_dead_letters(monkeypatch) -> None:
    repo = _wire_finalize(monkeypatch)
    monkeypatch.setattr(task, "run_pipeline", AsyncMock(side_effect=RuntimeError("boom")))
    result = await task.extract_event({}, "wrk_1", "evt_1")
    assert result == {"outcome": "failed"}
    assert repo.finalize_event.await_args.kwargs["outcome"] == "failed"
