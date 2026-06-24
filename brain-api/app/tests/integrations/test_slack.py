"""Unit tests for the Slack integration (OAuth v2 + polling + webhooks).

Provider HTTP calls are mocked; no real Slack API calls are made. The connector
programs against the SourceIntegration protocol, so these tests pass tokens and
secrets as plain strings (exactly how the sources service and sync jobs call it),
not as OAuthTokens objects.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config.settings import settings
from app.integrations.base import ChannelRef, RawItem, SourceIntegration
from app.integrations.slack import SlackAPIError, SlackIntegration


def _mock_http(method: str, json_payload: dict, status_code: int = 200):
    """Patch slack.http_client so ``method`` (get/post) returns ``json_payload``."""
    mock_client = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.json.return_value = json_payload
    getattr(mock_client, method).return_value = mock_resp
    patcher = patch("app.integrations.slack.http_client", return_value=mock_client)
    return patcher, mock_client


def _resp(status_code: int, json_payload: dict | None = None, headers: dict | None = None):
    """Build a single mock httpx response for multi-response (side_effect) tests."""
    r = MagicMock()
    r.status_code = status_code
    r.headers = headers or {}
    r.json.return_value = json_payload or {}
    return r


# ── OAuth ─────────────────────────────────────────────────────────────────────


def test_authorize_url_contains_required_params() -> None:
    url = SlackIntegration().authorize_url("test-state", "https://x/cb")
    assert "slack.com/oauth/v2/authorize" in url
    assert f"client_id={settings.slack_client_id}" in url
    assert "state=test-state" in url
    assert "channels%3Aread" in url
    assert "channels%3Ahistory" in url


@pytest.mark.asyncio
async def test_exchange_code_returns_bot_token_and_team_id() -> None:
    payload = {
        "ok": True,
        "access_token": "xoxb-token-123",
        "token_type": "bot",
        "scope": "channels:read,channels:history,users:read",
        "team": {"id": "T123", "name": "Acme"},
    }
    patcher, _ = _mock_http("post", payload)
    with patcher:
        tokens = await SlackIntegration().exchange_code("code", "https://x/cb")

    assert tokens.access_token == "xoxb-token-123"
    assert tokens.expires_at is None
    assert tokens.external_account_id == "T123"  # routes webhook deliveries
    assert tokens.scopes == ["channels:read", "channels:history", "users:read"]


@pytest.mark.asyncio
async def test_exchange_code_raises_on_not_ok() -> None:
    patcher, _ = _mock_http("post", {"ok": False, "error": "invalid_code"})
    with patcher, pytest.raises(ValueError, match="invalid_code"):
        await SlackIntegration().exchange_code("bad", "https://x/cb")


@pytest.mark.asyncio
async def test_refresh_is_unsupported() -> None:
    with pytest.raises(NotImplementedError):
        await SlackIntegration().refresh("anything")


# ── webhook verification ──────────────────────────────────────────────────────


def _signed(secret: str, timestamp: str, body: bytes) -> str:
    base = b"v0:" + timestamp.encode() + b":" + body
    return "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()


def test_verify_webhook_accepts_valid_signature() -> None:
    secret = "shhh"
    ts = str(int(time.time()))
    body = b'{"type":"event_callback"}'
    headers = {"X-Slack-Signature": _signed(secret, ts, body), "X-Slack-Request-Timestamp": ts}
    assert SlackIntegration().verify_webhook(headers, body, secret) is True


def test_verify_webhook_rejects_tampered_signature() -> None:
    ts = str(int(time.time()))
    headers = {"X-Slack-Signature": "v0=deadbeef", "X-Slack-Request-Timestamp": ts}
    assert SlackIntegration().verify_webhook(headers, b"{}", "shhh") is False


def test_verify_webhook_rejects_replay() -> None:
    secret = "shhh"
    old = str(int(time.time()) - 400)  # outside the 5-minute window
    body = b"{}"
    headers = {"X-Slack-Signature": _signed(secret, old, body), "X-Slack-Request-Timestamp": old}
    assert SlackIntegration().verify_webhook(headers, body, secret) is False


def test_verify_webhook_rejects_missing_header() -> None:
    ts = str(int(time.time()))
    headers = {"X-Slack-Request-Timestamp": ts}
    assert SlackIntegration().verify_webhook(headers, b"{}", "shhh") is False


def test_verify_webhook_rejects_empty_secret() -> None:
    ts = str(int(time.time()))
    headers = {"X-Slack-Signature": "v0=abc", "X-Slack-Request-Timestamp": ts}
    assert SlackIntegration().verify_webhook(headers, b"{}", "") is False


def test_verify_webhook_header_lookup_is_case_insensitive() -> None:
    secret = "shhh"
    ts = str(int(time.time()))
    body = b'{"ok":1}'
    headers = {"x-slack-signature": _signed(secret, ts, body), "x-slack-request-timestamp": ts}
    assert SlackIntegration().verify_webhook(headers, body, secret) is True


# ── channel listing ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_channels_paginates() -> None:
    page1 = {
        "ok": True,
        "channels": [{"id": "C1", "name": "general"}],
        "response_metadata": {"next_cursor": "CUR"},
    }
    page2 = {
        "ok": True,
        "channels": [{"id": "C2", "name": "random"}],
        "response_metadata": {"next_cursor": ""},
    }
    mock_client = AsyncMock()
    mock_client.get.side_effect = [_resp(200, page1), _resp(200, page2)]
    with patch("app.integrations.slack.http_client", return_value=mock_client):
        channels = await SlackIntegration().list_channels("xoxb-test")

    assert [c.external_id for c in channels] == ["C1", "C2"]
    assert mock_client.get.call_count == 2


@pytest.mark.asyncio
async def test_list_channels_raises_on_not_ok() -> None:
    patcher, _ = _mock_http("get", {"ok": False, "error": "invalid_auth"})
    with patcher, pytest.raises(RuntimeError, match="invalid_auth"):
        await SlackIntegration().list_channels("xoxb-bad")


# ── polling ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_since_collects_and_advances_cursor() -> None:
    integration = SlackIntegration()
    channels = [ChannelRef(external_id="C1", name="general")]
    history = {
        "ok": True,
        "has_more": False,
        "messages": [
            {"type": "message", "ts": "1700000002.0001", "user": "U1", "text": "newer"},
            {"type": "message", "ts": "1700000001.0001", "user": "U2", "text": "older"},
            {"type": "message", "subtype": "channel_join", "ts": "1700000003.0001"},
        ],
    }
    patcher, _ = _mock_http("get", history)
    with patcher, patch.object(integration, "list_channels", AsyncMock(return_value=channels)):
        items, cursor = await integration.fetch_since("xoxb-test", channels[0], None)

    # channel_join is skipped; two real messages collected; channel id injected.
    assert [i.external_id for i in items] == ["1700000002.0001", "1700000001.0001"]
    assert all(i.payload["channel"] == "C1" for i in items)
    assert cursor == "1700000002.0001"  # newest ts seen


@pytest.mark.asyncio
async def test_fetch_since_isolates_unreadable_channel() -> None:
    integration = SlackIntegration()
    channels = [
        ChannelRef(external_id="C_BAD", name="locked"),
        ChannelRef(external_id="C_OK", name="general"),
    ]

    async def fake_history(token, channel_id, oldest):
        if channel_id == "C_BAD":
            raise SlackAPIError("not_in_channel")
        item = RawItem(external_id="1700000005.0001", payload={"channel": channel_id})
        return [item], "1700000005.0001"

    with patch.object(integration, "list_channels", AsyncMock(return_value=channels)), patch.object(
        integration, "_history", side_effect=fake_history
    ):
        items, cursor = await integration.fetch_since("xoxb-test", channels[0], None)

    # The bad channel is skipped, the good one still contributes.
    assert len(items) == 1
    assert cursor == "1700000005.0001"


# ── rate limiting (HTTP 429 + Retry-After) ─────────────────────────────────────


@pytest.mark.asyncio
async def test_request_honors_retry_after_then_succeeds() -> None:
    """A 429 with Retry-After is slept off (bounded) and the retry succeeds."""
    integration = SlackIntegration()
    ok_body = {"ok": True, "channels": [], "response_metadata": {"next_cursor": ""}}
    mock_client = AsyncMock()
    mock_client.get.side_effect = [
        _resp(429, headers={"Retry-After": "1"}),
        _resp(200, ok_body),
    ]
    with patch("app.integrations.slack.http_client", return_value=mock_client), patch(
        "app.integrations.slack.asyncio.sleep", AsyncMock()
    ) as sleep:
        await integration.list_channels("xoxb-test")

    sleep.assert_awaited_once_with(1)  # honored the Retry-After header
    assert mock_client.get.call_count == 2


@pytest.mark.asyncio
async def test_fetch_since_holds_cursor_on_persistent_rate_limit() -> None:
    """When a channel stays 429 past the retry budget, the cursor is NOT advanced."""
    integration = SlackIntegration()
    channels = [ChannelRef(external_id="C1", name="general")]
    mock_client = AsyncMock()
    mock_client.get.return_value = _resp(429, headers={"Retry-After": "1"})
    with patch("app.integrations.slack.http_client", return_value=mock_client), patch(
        "app.integrations.slack.asyncio.sleep", AsyncMock()
    ), patch.object(integration, "list_channels", AsyncMock(return_value=channels)):
        items, cursor = await integration.fetch_since("xoxb-test", channels[0], "1699999999.0001")

    assert items == []
    assert cursor == "1699999999.0001"  # held, not advanced past unfetched messages


@pytest.mark.asyncio
async def test_fetch_since_reraises_auth_error() -> None:
    """An auth-class ok:false surfaces so source_sync can mark the connection error."""
    integration = SlackIntegration()
    channels = [ChannelRef(external_id="C1", name="general")]

    async def fake_history(token, channel_id, oldest):
        raise SlackAPIError("token_revoked", auth=True)

    with patch.object(integration, "list_channels", AsyncMock(return_value=channels)), patch.object(
        integration, "_history", side_effect=fake_history
    ), pytest.raises(SlackAPIError, match="token_revoked"):
        await integration.fetch_since("xoxb-test", channels[0], None)


# ── normalize ──────────────────────────────────────────────────────────────────


def test_normalize_webhook_envelope() -> None:
    payload = {
        "type": "event_callback",
        "team_id": "T789",
        "event": {
            "type": "message",
            "ts": "1700000000.001234",
            "channel": "C123",
            "user": "U456",
            "text": "hello world",
        },
    }
    event = SlackIntegration().normalize(RawItem(external_id="x", payload=payload))
    assert event.provider == "slack"
    assert event.event_type == "message"
    assert event.actor["id"] == "U456"
    assert event.content == "hello world"
    assert event.source_id == "1700000000.001234"
    assert event.external_event_id == "slack:C123:1700000000.001234"
    assert "T789" in event.url


def test_normalize_polled_message() -> None:
    # Bare message as produced by fetch_since (channel injected, no envelope).
    payload = {
        "type": "message", "ts": "1700000000.0001", "channel": "C9", "user": "U9", "text": "hi",
    }
    event = SlackIntegration().normalize(RawItem(external_id="1700000000.0001", payload=payload))
    assert event.external_event_id == "slack:C9:1700000000.0001"
    assert event.content == "hi"


def test_normalize_rejects_bot_message() -> None:
    payload = {
        "type": "event_callback",
        "team_id": "T1",
        "event": {"type": "message", "subtype": "bot_message", "ts": "1.0", "channel": "C1"},
    }
    with pytest.raises(ValueError):
        SlackIntegration().normalize(RawItem(external_id="x", payload=payload))


def test_normalize_rejects_url_verification() -> None:
    payload = {"type": "url_verification", "challenge": "abc"}
    with pytest.raises(ValueError):
        SlackIntegration().normalize(RawItem(external_id="x", payload=payload))


# ── protocol conformance ───────────────────────────────────────────────────────


def test_slack_integration_implements_protocol() -> None:
    integration = SlackIntegration()
    assert isinstance(integration, SourceIntegration)
    assert integration.provider == "slack"
