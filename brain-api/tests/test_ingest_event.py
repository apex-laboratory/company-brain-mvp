"""
Tests for POST /ingest/event.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from unittest.mock import patch

from routers.ingest import router

_app = FastAPI()
_app.include_router(router, prefix="/ingest")


def _make_pool(fetchval_return=None):
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=fetchval_return or uuid4())

    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return pool, conn


async def test_post_event_returns_200_with_id():
    event_id = uuid4()
    pool, _ = _make_pool(fetchval_return=event_id)

    with patch("routers.ingest.get_pool", AsyncMock(return_value=pool)):
        async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as client:
            resp = await client.post("/ingest/event", json={
                "source": "slack",
                "event_type": "message",
                "source_id": "C123/1234.5678",
                "payload": {"text": "we should always escalate P0 within 15 minutes"},
            })

    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == str(event_id)
    assert body["status"] == "queued"


async def test_post_event_inserts_to_source_events():
    """Correct columns and values reach the INSERT statement."""
    pool, conn = _make_pool()

    with patch("routers.ingest.get_pool", AsyncMock(return_value=pool)):
        async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as client:
            await client.post("/ingest/event", json={
                "source": "notion",
                "event_type": "page_update",
                "source_id": "page-abc",
                "payload": {"page_type": "designated_policy_page"},
            })

    import json
    call_args = conn.fetchval.call_args[0]
    assert "INSERT INTO source_events" in call_args[0]
    assert call_args[1] == "notion"
    assert call_args[2] == "page_update"
    assert call_args[3] == "page-abc"
    stored_payload = json.loads(call_args[4])
    assert stored_payload["page_type"] == "designated_policy_page"


async def test_post_event_minimal_payload():
    """Empty payload (omitted) defaults to {} and is accepted."""
    pool, _ = _make_pool()

    with patch("routers.ingest.get_pool", AsyncMock(return_value=pool)):
        async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as client:
            resp = await client.post("/ingest/event", json={
                "source": "github",
                "event_type": "pr_merged",
                "source_id": "pr-99",
            })

    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"
