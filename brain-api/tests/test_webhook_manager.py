"""
Tests for services/webhook_manager.py — subscribe() and unsubscribe().
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4
from datetime import datetime, timezone

import pytest

from services.webhook_manager import subscribe, unsubscribe


def _make_pool(fetchrow_return=None, fetchval_return=None):
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=fetchrow_return)
    conn.fetchval = AsyncMock(return_value=fetchval_return)
    conn.execute = AsyncMock()

    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return pool, conn


def _subscription_row(source="slack", target_id="C123", source_ref_id="ext-sub-1"):
    return {
        "id": uuid4(),
        "source": source,
        "source_ref_id": source_ref_id,
        "target_id": target_id,
        "status": "active",
        "created_at": datetime.now(tz=timezone.utc),
    }


# ── subscribe ─────────────────────────────────────────────────────────────────

async def test_subscribe_inserts_row():
    """Happy path: connector returns a ref id → INSERT into webhook_subscriptions."""
    row = _subscription_row(source_ref_id="slack-sub-xyz")
    pool, conn = _make_pool(fetchrow_return=row)

    with patch(
        "services.webhook_manager._get_connector"
    ) as mock_get:
        mock_connector = AsyncMock()
        mock_connector.subscribe_webhook = AsyncMock(return_value="slack-sub-xyz")
        mock_get.return_value = mock_connector

        result = await subscribe(pool, "slack", "C123", "xoxb-token", "https://cb.example.com/ingest/event")

    assert result["source_ref_id"] == "slack-sub-xyz"
    assert result["target_id"] == "C123"
    assert "INSERT INTO webhook_subscriptions" in conn.fetchrow.call_args[0][0]


async def test_subscribe_connector_not_implemented():
    """NotImplementedError → stub source_ref_id used, INSERT still happens."""
    row = _subscription_row(source_ref_id="stub-slack-C123")
    pool, conn = _make_pool(fetchrow_return=row)

    with patch("services.webhook_manager._get_connector") as mock_get:
        mock_connector = AsyncMock()
        mock_connector.subscribe_webhook = AsyncMock(side_effect=NotImplementedError)
        mock_get.return_value = mock_connector

        result = await subscribe(pool, "slack", "C123", "tok", "https://cb.example.com/ingest/event")

    assert result["source_ref_id"] == "stub-slack-C123"
    conn.fetchrow.assert_called_once()


async def test_subscribe_unknown_source_raises():
    """`ValueError` from _get_connector propagates before any DB call."""
    pool, conn = _make_pool()

    with pytest.raises(ValueError, match="Unknown source"):
        await subscribe(pool, "gdrive", "folder-1", "tok", "https://cb.example.com/ingest/event")

    conn.fetchrow.assert_not_called()


# ── unsubscribe ───────────────────────────────────────────────────────────────

async def test_unsubscribe_active_subscription():
    """Active subscription found → connector called + DB marked revoked."""
    existing = {"id": uuid4(), "source_ref_id": "ext-sub-1"}
    pool, conn = _make_pool(fetchrow_return=existing)

    with patch("services.webhook_manager._get_connector") as mock_get:
        mock_connector = AsyncMock()
        mock_connector.unsubscribe_webhook = AsyncMock()
        mock_get.return_value = mock_connector

        await unsubscribe(pool, "slack", "C123", "tok")

    mock_connector.unsubscribe_webhook.assert_called_once_with("ext-sub-1")
    assert "UPDATE webhook_subscriptions SET status='revoked'" in conn.execute.call_args[0][0]


async def test_unsubscribe_no_active_subscription():
    """No active subscription → returns early without any UPDATE."""
    pool, conn = _make_pool(fetchrow_return=None)

    with patch("services.webhook_manager._get_connector") as mock_get:
        mock_connector = AsyncMock()
        mock_get.return_value = mock_connector

        await unsubscribe(pool, "slack", "C999", "tok")

    conn.execute.assert_not_called()
    mock_connector.unsubscribe_webhook.assert_not_called()


async def test_unsubscribe_connector_not_implemented():
    """NotImplementedError from connector → DB still marked revoked."""
    existing = {"id": uuid4(), "source_ref_id": "ext-sub-1"}
    pool, conn = _make_pool(fetchrow_return=existing)

    with patch("services.webhook_manager._get_connector") as mock_get:
        mock_connector = AsyncMock()
        mock_connector.unsubscribe_webhook = AsyncMock(side_effect=NotImplementedError)
        mock_get.return_value = mock_connector

        await unsubscribe(pool, "notion", "page-1", "tok")

    assert "UPDATE webhook_subscriptions SET status='revoked'" in conn.execute.call_args[0][0]
