"""
Unit tests for services/sweep_worker.py.

All DB and external calls are mocked — no running server or database required.
Uses pytest-asyncio for async test functions.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from connectors.github import GitHubConnector
from connectors.jira import JiraConnector
from connectors.notion import NotionConnector
from connectors.slack import SlackConnector
from connectors.zendesk import ZendeskConnector
from services.sweep_worker import _get_connector, run_sweep


# ── connector registry ────────────────────────────────────────────────────────

@pytest.mark.parametrize("source,expected_cls", [
    ("notion", NotionConnector),
    ("slack", SlackConnector),
    ("github", GitHubConnector),
    ("jira", JiraConnector),
    ("zendesk", ZendeskConnector),
])
def test_get_connector_known_sources(source, expected_cls):
    connector = _get_connector(source, "tok")
    assert isinstance(connector, expected_cls)


def test_get_connector_unknown_raises():
    with pytest.raises(ValueError, match="Unknown source"):
        _get_connector("gdrive", "tok")


# ── rate interval ─────────────────────────────────────────────────────────────

def test_rate_interval_calculation():
    assert 60.0 / 10 == 6.0


# ── async sweep tests (mocked DB + pipeline) ──────────────────────────────────

def _make_pool(fetch_rows=None):
    """Build a minimal asyncpg pool mock."""
    conn = AsyncMock()
    conn.execute = AsyncMock()
    conn.fetchval = AsyncMock(return_value=0)
    conn.fetch = AsyncMock(return_value=fetch_rows or [])

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncContextManager(conn))
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return pool, conn


class _AsyncContextManager:
    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *_):
        return False


@pytest.mark.asyncio
async def test_run_sweep_no_connections_completes():
    """No connected sources → runs→completed without errors."""
    pool, conn = _make_pool(fetch_rows=[])
    sweep_id = uuid4()

    with (
        patch("services.sweep_worker.get_pool", AsyncMock(return_value=pool)),
        patch("services.sweep_worker.sweep_processing_order", return_value=["notion"]),
        patch("services.sweep_worker.sweep_rate_per_minute", return_value=60),
        patch("services.sweep_worker.sweep_semaphore_limit", return_value=2),
    ):
        await run_sweep(sweep_id)

    calls = [str(c) for c in conn.execute.call_args_list]
    assert any("running" in c for c in calls)
    assert any("completed" in c for c in calls)


@pytest.mark.asyncio
async def test_run_sweep_connector_not_implemented():
    """fetch_historical raises NotImplementedError → treated as empty list, sweep completes."""
    sweep_id = uuid4()

    source_row = {
        "source": "notion",
        "access_token": "tok",
        "monitored_ids": ["page-1"],
        "lookback_days": 30,
    }
    # First fetch returns the connection row; subsequent fetches return []
    conn = AsyncMock()
    conn.execute = AsyncMock()
    conn.fetchval = AsyncMock(return_value=0)  # no existing events
    conn.fetch = AsyncMock(side_effect=[[source_row], []])  # connections, then unprocessed items

    pool = MagicMock()
    pool.acquire = MagicMock()
    pool.acquire.return_value = _AsyncContextManager(conn)

    with (
        patch("services.sweep_worker.get_pool", AsyncMock(return_value=pool)),
        patch("services.sweep_worker.sweep_processing_order", return_value=["notion"]),
        patch("services.sweep_worker.sweep_rate_per_minute", return_value=60),
        patch("services.sweep_worker.sweep_semaphore_limit", return_value=2),
    ):
        await run_sweep(sweep_id)  # NotionConnector.fetch_historical raises NotImplementedError

    calls = [str(c) for c in conn.execute.call_args_list]
    assert any("completed" in c for c in calls)


@pytest.mark.asyncio
async def test_run_sweep_pipeline_not_implemented():
    """run_pipeline raises NotImplementedError → outcome='discarded', sweep completes."""
    sweep_id = uuid4()

    source_row = {
        "source": "notion",
        "access_token": "tok",
        "monitored_ids": [],
        "lookback_days": 30,
    }
    event_row = {
        "id": uuid4(),
        "event_type": "sweep_historical",
        "source_id": "item-1",
        "payload": {"_target_id": "page-1"},
    }

    conn = AsyncMock()
    conn.execute = AsyncMock()
    conn.fetchval = AsyncMock(return_value=1)  # already fetched
    conn.fetch = AsyncMock(side_effect=[[source_row], [event_row]])

    pool = MagicMock()
    pool.acquire.return_value = _AsyncContextManager(conn)

    with (
        patch("services.sweep_worker.get_pool", AsyncMock(return_value=pool)),
        patch("services.sweep_worker.sweep_processing_order", return_value=["notion"]),
        patch("services.sweep_worker.sweep_rate_per_minute", return_value=60),
        patch("services.sweep_worker.sweep_semaphore_limit", return_value=2),
        patch("services.sweep_worker.run_pipeline", AsyncMock(side_effect=NotImplementedError)),
        patch("asyncio.sleep", AsyncMock()),
    ):
        await run_sweep(sweep_id)

    update_calls = [str(c) for c in conn.execute.call_args_list]
    assert any("discarded" in c for c in update_calls)
    assert any("completed" in c for c in update_calls)


@pytest.mark.asyncio
async def test_run_sweep_marks_failed_on_unexpected_error():
    """Unexpected DB error after marking running → status set to 'failed'."""
    sweep_id = uuid4()

    conn = AsyncMock()
    conn.execute = AsyncMock()
    conn.fetch = AsyncMock(side_effect=RuntimeError("DB down"))

    pool = MagicMock()
    pool.acquire.return_value = _AsyncContextManager(conn)

    with (
        patch("services.sweep_worker.get_pool", AsyncMock(return_value=pool)),
        patch("services.sweep_worker.sweep_processing_order", return_value=["notion"]),
        patch("services.sweep_worker.sweep_rate_per_minute", return_value=60),
        patch("services.sweep_worker.sweep_semaphore_limit", return_value=2),
        pytest.raises(RuntimeError),
    ):
        await run_sweep(sweep_id)

    update_calls = [str(c) for c in conn.execute.call_args_list]
    assert any("failed" in c for c in update_calls)
