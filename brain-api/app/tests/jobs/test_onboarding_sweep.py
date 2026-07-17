"""onboarding_sweep orchestration tests (KAN-2, PRD Phase 2 acceptance).

Covers the three sweep guarantees:

* **authority order** — sources run in ``source_authority.yaml`` priority order;
* **failure isolation** — one source failing is recorded and the rest still run;
* **resume** — a retried sweep skips sources already marked ``completed``.

DB access is mocked at the repository boundary; ``source_sync`` is stubbed so no
network or crypto is involved.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.jobs.tasks import onboarding_sweep as job


class _AsyncCtx:
    def __init__(self, value: object) -> None:
        self._value = value

    async def __aenter__(self) -> object:
        return self._value

    async def __aexit__(self, *_: object) -> bool:
        return False


def _repo(sources: list[dict], sweep: dict | None) -> MagicMock:
    return MagicMock(
        get_sweep=AsyncMock(return_value=sweep),
        list_connected_sources=AsyncMock(return_value=sources),
        set_sweep_status=AsyncMock(),
        update_sweep_source_progress=AsyncMock(),
    )


_FRESH_SWEEP: dict = {"id": "swp", "status": "pending", "progress": {}}


async def _run(
    sources: list[dict],
    sync_results: dict[str, object],
    *,
    sweep: dict | None = _FRESH_SWEEP,
) -> tuple[dict, MagicMock, list[str]]:
    """Run the job with stubbed collaborators. Returns (result, repo, sync order)."""
    repo = _repo(sources, sweep)
    synced: list[str] = []

    async def fake_sync(
        ctx: dict, workspace_id: str, source_id: str, sweep_id: str | None = None
    ) -> dict:
        synced.append(source_id)
        assert sweep_id == "swp_1"  # sweep stamps events for the batched extract pass
        outcome = sync_results.get(source_id, {"inserted": 0})
        if isinstance(outcome, Exception):
            raise outcome
        return outcome  # type: ignore[return-value]

    session = MagicMock(commit=AsyncMock())
    with patch.object(job, "get_session", return_value=_AsyncCtx(session)), patch.object(
        job, "run_in_tenant", return_value=_AsyncCtx(None)
    ), patch.object(job, "_repo", repo), patch.object(
        job, "source_sync", fake_sync
    ), patch.object(
        job, "processing_order", return_value=["notion", "google_drive", "slack"]
    ), patch.object(job, "enqueue", AsyncMock()) as enqueue:
        result = await job.onboarding_sweep({}, "wrk_1", "swp_1")
    return result, repo, synced, enqueue


@pytest.mark.asyncio
async def test_sources_run_in_authority_order() -> None:
    sources = [
        {"id": "src_slack", "provider": "slack"},
        {"id": "src_notion", "provider": "notion"},
        {"id": "src_drive", "provider": "google_drive"},
    ]
    result, _, synced, _enq = await _run(sources, {})
    assert synced == ["src_notion", "src_drive", "src_slack"]
    assert result["status"] == "completed"


@pytest.mark.asyncio
async def test_one_source_failing_does_not_halt_the_rest() -> None:
    sources = [
        {"id": "src_notion", "provider": "notion"},
        {"id": "src_slack", "provider": "slack"},
    ]
    result, repo, synced, _enq = await _run(
        sources, {"src_notion": RuntimeError("provider down")}
    )
    assert synced == ["src_notion", "src_slack"]  # slack still ran
    assert result == {"sources": 2, "failed": 1, "status": "completed"}

    writes = {
        call.args[2]: call.args[3]
        for call in repo.update_sweep_source_progress.await_args_list
    }
    assert writes["notion"]["status"] == "failed"
    assert writes["slack"]["status"] == "completed"
    # Sweep itself completes despite the failure (isolation, not all-or-nothing).
    final = repo.set_sweep_status.await_args_list[-1]
    assert final.args[2] == "completed" and final.kwargs["completed"] is True


@pytest.mark.asyncio
async def test_auth_broken_source_recorded_as_failed() -> None:
    sources = [{"id": "src_slack", "provider": "slack"}]
    result, repo, _, _enq = await _run(
        sources, {"src_slack": {"inserted": 3, "error": "auth_broken"}}
    )
    assert result["failed"] == 1
    writes = {
        call.args[2]: call.args[3]
        for call in repo.update_sweep_source_progress.await_args_list
    }
    assert writes["slack"] == {"status": "failed", "inserted": 3, "error": "auth_broken"}


@pytest.mark.asyncio
async def test_all_sources_failing_marks_sweep_failed() -> None:
    sources = [{"id": "src_notion", "provider": "notion"}]
    result, repo, _, _enq = await _run(sources, {"src_notion": RuntimeError("down")})
    assert result["status"] == "failed"
    final = repo.set_sweep_status.await_args_list[-1]
    assert final.args[2] == "failed"


@pytest.mark.asyncio
async def test_resume_skips_sources_already_completed() -> None:
    sources = [
        {"id": "src_notion", "provider": "notion"},
        {"id": "src_slack", "provider": "slack"},
    ]
    sweep = {
        "id": "swp",
        "status": "running",
        "progress": {"notion": {"status": "completed", "inserted": 12}},
    }
    result, _, synced, _enq = await _run(sources, {}, sweep=sweep)
    assert synced == ["src_slack"]  # notion skipped on the retry
    assert result == {"sources": 1, "failed": 0, "status": "completed"}


@pytest.mark.asyncio
async def test_missing_sweep_row_is_a_noop() -> None:
    result, _, synced, enq = await _run([], {}, sweep=None)
    # sweep=None short-circuits before set_sweep_status (and before enqueue)
    assert result == {"error": "sweep_not_found"}
    assert synced == []
    enq.assert_not_awaited()


@pytest.mark.asyncio
async def test_enqueues_sweep_extract_after_ingestion() -> None:
    sources = [{"id": "src_notion", "provider": "notion"}]
    _result, _repo, _synced, enq = await _run(sources, {})
    # Ingestion done → the batched extraction pass is handed the sweep.
    enq.assert_awaited_once_with("sweep_extract", "wrk_1", "swp_1")
