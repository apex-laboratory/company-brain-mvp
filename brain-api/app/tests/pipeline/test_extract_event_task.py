"""Unit tests for the extract_event ARQ task — thin wrapper over run_event_safely."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from app.jobs.tasks import extract_event as task
from app.pipeline.types import PipelineResult


async def test_returns_pipeline_outcome() -> None:
    with patch.object(
        task, "run_event_safely",
        AsyncMock(return_value=PipelineResult(outcome="published", skill_id="skl_1")),
    ) as run:
        result = await task.extract_event({}, "wrk_1", "evt_1")
    assert result == {"outcome": "published", "skill_id": "skl_1"}
    assert run.await_args.kwargs["sweep_sourced"] is False


async def test_sweep_id_marks_sweep_sourced() -> None:
    with patch.object(
        task, "run_event_safely",
        AsyncMock(return_value=PipelineResult(outcome="review")),
    ) as run:
        await task.extract_event({}, "wrk_1", "evt_1", sweep_id="swp_1")
    assert run.await_args.kwargs["sweep_sourced"] is True
