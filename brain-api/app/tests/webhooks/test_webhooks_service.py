"""Webhooks receiver tests — the signature/handshake/enqueue boundary.

Covers the Slack Events API ``url_verification`` handshake (review bug #2): the
receiver must echo the challenge synchronously and NOT enqueue it, or the endpoint
can never be registered in the Slack app console.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules.webhooks.service import WebhooksService
from app.shared.errors.app_error import UnauthorizedError


def _integration(verify: bool = True) -> MagicMock:
    m = MagicMock()
    m.verify_webhook.return_value = verify
    return m


@pytest.mark.asyncio
async def test_receive_echoes_url_verification_challenge_without_enqueue() -> None:
    body = json.dumps({"type": "url_verification", "challenge": "abc123"}).encode()
    with patch(
        "app.modules.webhooks.service.get_integration", return_value=_integration()
    ), patch("app.modules.webhooks.service.enqueue", AsyncMock()) as enq:
        challenge = await WebhooksService().receive("slack", {}, body)

    assert challenge == "abc123"  # echoed verbatim by the router
    enq.assert_not_awaited()  # handshake is not an ingestible event


@pytest.mark.asyncio
async def test_receive_enqueues_normal_event() -> None:
    body = json.dumps({"type": "event_callback", "event": {"type": "message"}}).encode()
    with patch(
        "app.modules.webhooks.service.get_integration", return_value=_integration()
    ), patch("app.modules.webhooks.service.enqueue", AsyncMock()) as enq:
        result = await WebhooksService().receive("slack", {}, body)

    assert result is None
    enq.assert_awaited_once()


@pytest.mark.asyncio
async def test_receive_rejects_bad_signature() -> None:
    body = json.dumps({"type": "url_verification", "challenge": "abc123"}).encode()
    with patch(
        "app.modules.webhooks.service.get_integration",
        return_value=_integration(verify=False),
    ), patch("app.modules.webhooks.service.enqueue", AsyncMock()) as enq, pytest.raises(
        UnauthorizedError
    ):
        await WebhooksService().receive("slack", {}, body)
    enq.assert_not_awaited()
