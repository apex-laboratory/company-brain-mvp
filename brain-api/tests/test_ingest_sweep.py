"""
Tests for POST /ingest/sweep and GET /ingest/sweep/{id}/status.

Uses a minimal FastAPI app with just the ingest router to avoid running the
full lifespan (DB pool init, Redis, MCP server). DB calls are mocked via
patch on routers.ingest.get_pool.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from routers.ingest import router

# Minimal app — no lifespan, no other routers
_app = FastAPI()
_app.include_router(router, prefix="/ingest")


def _make_sweep_row(sweep_id=None, sources=None, lookback_days=180):
    """Build a fake asyncpg row-like dict matching the RETURNING columns."""
    sid = sweep_id or uuid4()
    config = {"sources": sources or ["notion", "github", "jira", "slack", "zendesk"],
               "lookback_days": lookback_days}
    return {
        "id": sid,
        "status": "running",
        "progress": {},
        "skills_created": 0,
        "skills_queued": 0,
        "started_at": datetime.now(tz=timezone.utc),
        "completed_at": None,
        "_config": config,
    }


def _make_pool(row: dict):
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=row)

    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return pool, conn


# ── tests ────────────────────────────────────────────────────────────────────

async def test_post_sweep_returns_202():
    row = _make_sweep_row()
    pool, _ = _make_pool(row)

    with (
        patch("routers.ingest.get_pool", AsyncMock(return_value=pool)),
        patch("routers.ingest.asyncio.create_task", side_effect=lambda c: c.close()),
    ):
        async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as client:
            resp = await client.post("/ingest/sweep", json={})

    assert resp.status_code == 202
    body = resp.json()
    assert body["id"] == str(row["id"])
    assert body["status"] == "running"
    assert body["skills_created"] == 0
    assert body["skills_queued"] == 0
    assert body["completed_at"] is None


async def test_post_sweep_launches_background_task():
    row = _make_sweep_row()
    pool, _ = _make_pool(row)

    with (
        patch("routers.ingest.get_pool", AsyncMock(return_value=pool)),
        patch("routers.ingest.asyncio.create_task") as mock_create_task,
    ):
        async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as client:
            await client.post("/ingest/sweep", json={})

    assert mock_create_task.call_count == 1
    # The coroutine passed to create_task should be a run_sweep coroutine
    coro = mock_create_task.call_args[0][0]
    assert "run_sweep" in type(coro).__qualname__ or hasattr(coro, "cr_code")
    coro.close()  # clean up the unawaited coroutine


async def test_post_sweep_default_config_stored():
    """Empty body → SweepConfig defaults (all 5 sources, 180 days) stored in DB."""
    row = _make_sweep_row()
    pool, conn = _make_pool(row)

    with (
        patch("routers.ingest.get_pool", AsyncMock(return_value=pool)),
        patch("routers.ingest.asyncio.create_task", side_effect=lambda c: c.close()),
    ):
        async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as client:
            await client.post("/ingest/sweep", json={})

    import json
    call_args = conn.fetchrow.call_args
    stored_config = json.loads(call_args[0][1])
    assert stored_config["sources"] == ["notion", "github", "jira", "slack", "zendesk"]
    assert stored_config["lookback_days"] == 180


async def test_post_sweep_custom_sources():
    """Custom sources and lookback_days are stored correctly."""
    row = _make_sweep_row(sources=["notion"], lookback_days=30)
    pool, conn = _make_pool(row)

    with (
        patch("routers.ingest.get_pool", AsyncMock(return_value=pool)),
        patch("routers.ingest.asyncio.create_task", side_effect=lambda c: c.close()),
    ):
        async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as client:
            await client.post("/ingest/sweep", json={"sources": ["notion"], "lookback_days": 30})

    import json
    stored_config = json.loads(conn.fetchrow.call_args[0][1])
    assert stored_config["sources"] == ["notion"]
    assert stored_config["lookback_days"] == 30


# ── GET /ingest/sweep/{id}/status ────────────────────────────────────────────

async def test_get_sweep_status_running():
    """Running sweep with partial progress → 200 with populated sources dict."""
    sweep_id = uuid4()
    row = {
        "id": sweep_id,
        "status": "running",
        "progress": {
            "notion": {"total": 50, "processed": 20, "published": 0, "queued": 18, "discarded": 2},
        },
        "skills_created": 0,
        "skills_queued": 18,
        "started_at": datetime.now(tz=timezone.utc),
        "completed_at": None,
    }
    pool, _ = _make_pool(row)

    with patch("routers.ingest.get_pool", AsyncMock(return_value=pool)):
        async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as client:
            resp = await client.get(f"/ingest/sweep/{sweep_id}/status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "running"
    assert body["sources"]["notion"]["processed"] == 20
    assert body["sources"]["notion"]["queued"] == 18


async def test_get_sweep_status_completed():
    """Completed sweep has completed_at populated and status='completed'."""
    sweep_id = uuid4()
    now = datetime.now(tz=timezone.utc)
    row = {
        "id": sweep_id,
        "status": "completed",
        "progress": {"slack": {"total": 10, "processed": 10, "published": 0, "queued": 8, "discarded": 2}},
        "skills_created": 0,
        "skills_queued": 8,
        "started_at": now,
        "completed_at": now,
    }
    pool, _ = _make_pool(row)

    with patch("routers.ingest.get_pool", AsyncMock(return_value=pool)):
        async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as client:
            resp = await client.get(f"/ingest/sweep/{sweep_id}/status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "completed"
    assert body["completed_at"] is not None


async def test_get_sweep_status_empty_progress():
    """progress={} (worker hasn't started yet) → 200 with sources={}."""
    sweep_id = uuid4()
    row = {
        "id": sweep_id,
        "status": "running",
        "progress": {},
        "skills_created": 0,
        "skills_queued": 0,
        "started_at": datetime.now(tz=timezone.utc),
        "completed_at": None,
    }
    pool, _ = _make_pool(row)

    with patch("routers.ingest.get_pool", AsyncMock(return_value=pool)):
        async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as client:
            resp = await client.get(f"/ingest/sweep/{sweep_id}/status")

    assert resp.status_code == 200
    assert resp.json()["sources"] == {}


async def test_get_sweep_status_not_found():
    """Unknown sweep_id → 404."""
    pool, conn = _make_pool(None)
    conn.fetchrow = AsyncMock(return_value=None)

    with patch("routers.ingest.get_pool", AsyncMock(return_value=pool)):
        async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as client:
            resp = await client.get(f"/ingest/sweep/{uuid4()}/status")

    assert resp.status_code == 404
