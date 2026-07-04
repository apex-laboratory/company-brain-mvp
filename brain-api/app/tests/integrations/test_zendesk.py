"""Unit tests for the Zendesk integration (OAuth 2.0 + incremental polling).

Provider HTTP calls are mocked; no real Zendesk API calls are made. The connector
programs against the SourceIntegration protocol, so tests pass tokens/secrets as
plain strings (how the sources service and sync jobs call it). Zendesk is
subdomain-scoped: the subdomain reaches ``authorize_url`` via ``config``, reaches
``exchange_code`` via ``installation_id``, and reaches ``fetch_since`` via the
synthetic ``channel.external_id``.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.config.settings import settings
from app.integrations.base import ChannelRef, RawItem, SourceIntegration
from app.integrations.zendesk import ZendeskIntegration


def _resp(status_code: int, json_payload: dict | None = None, headers: dict | None = None):
    r = MagicMock()
    r.status_code = status_code
    r.headers = headers or {}
    r.json.return_value = json_payload or {}
    return r


def _client_returning(*responses):
    """Patch zendesk.http_client so GET yields ``responses`` in order."""
    mock_client = AsyncMock()
    mock_client.get.side_effect = list(responses)
    return patch("app.integrations.zendesk.http_client", return_value=mock_client), mock_client


# ── OAuth ─────────────────────────────────────────────────────────────────────


def test_authorize_url_is_subdomain_scoped() -> None:
    url = ZendeskIntegration().authorize_url(
        "st", "https://x/cb", config={"subdomain": "acme"}
    )
    assert url.startswith("https://acme.zendesk.com/oauth/authorizations/new?")
    assert f"client_id={settings.zendesk_client_id}" in url
    assert "response_type=code" in url
    assert "state=st" in url


def test_authorize_url_requires_subdomain() -> None:
    with pytest.raises(ValueError, match="subdomain"):
        ZendeskIntegration().authorize_url("st", "https://x/cb", config=None)


@pytest.mark.asyncio
async def test_exchange_code_stores_subdomain_as_account() -> None:
    payload = {"access_token": "tok-123", "token_type": "bearer", "scope": "read"}
    mock_client = AsyncMock()
    mock_client.post.return_value = _resp(200, payload)
    with patch("app.integrations.zendesk.http_client", return_value=mock_client):
        tokens = await ZendeskIntegration().exchange_code(
            "code", "https://x/cb", installation_id="acme"
        )

    assert tokens.access_token == "tok-123"
    assert tokens.expires_at is None
    assert tokens.external_account_id == "acme"  # subdomain feeds fetch_since
    assert tokens.scopes == ["read"]
    # token endpoint is the subdomain's host
    assert mock_client.post.call_args.args[0] == "https://acme.zendesk.com/oauth/tokens"


@pytest.mark.asyncio
async def test_exchange_code_requires_subdomain() -> None:
    with pytest.raises(ValueError, match="subdomain"):
        await ZendeskIntegration().exchange_code("code", "https://x/cb", installation_id=None)


@pytest.mark.asyncio
async def test_refresh_is_unsupported() -> None:
    with pytest.raises(NotImplementedError):
        await ZendeskIntegration().refresh("anything")


# ── polling (time-based incremental export) ────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_since_single_page_advances_cursor() -> None:
    end_time = 1782837600
    page = {
        "tickets": [
            {"id": 1, "subject": "a", "updated_at": "2026-06-30T10:00:00Z"},
            {"id": 2, "subject": "b", "updated_at": "2026-06-30T11:00:00Z"},
        ],
        "end_time": end_time,
        "end_of_stream": True,
    }
    patcher, client = _client_returning(_resp(200, page))
    with patcher:
        items, cursor = await ZendeskIntegration().fetch_since(
            "tok", ChannelRef(external_id="acme", name="ws"), None
        )

    assert [i.external_id for i in items] == ["1", "2"]
    assert all(i.payload["_subdomain"] == "acme" for i in items)  # injected for normalize
    assert cursor == datetime.fromtimestamp(end_time, tz=UTC).isoformat()
    assert client.get.call_count == 1
    # initial sync starts at epoch
    assert client.get.call_args.kwargs["params"]["start_time"] == 0


@pytest.mark.asyncio
async def test_fetch_since_passes_cursor_as_start_time() -> None:
    cur = "2026-06-30T12:00:00+00:00"
    start = int(datetime.fromisoformat(cur).timestamp())
    page = {"tickets": [], "end_time": start, "end_of_stream": True}
    patcher, client = _client_returning(_resp(200, page))
    with patcher:
        await ZendeskIntegration().fetch_since(
            "tok", ChannelRef(external_id="acme", name="ws"), cur
        )

    assert client.get.call_args.kwargs["params"]["start_time"] == start


@pytest.mark.asyncio
async def test_fetch_since_paginates_until_end_of_stream() -> None:
    from app.integrations.zendesk import _PAGE_SIZE

    full = [
        {"id": i, "subject": "x", "updated_at": "2026-06-30T10:00:00Z"}
        for i in range(_PAGE_SIZE)
    ]
    page1 = {"tickets": full, "end_time": 1782837600, "end_of_stream": False}
    page2 = {
        "tickets": [{"id": 99999, "subject": "last", "updated_at": "2026-06-30T12:00:00Z"}],
        "end_time": 1782841200,
        "end_of_stream": True,
    }
    patcher, client = _client_returning(_resp(200, page1), _resp(200, page2))
    with patcher:
        items, cursor = await ZendeskIntegration().fetch_since(
            "tok", ChannelRef(external_id="acme", name="ws"), None
        )

    assert client.get.call_count == 2
    assert len(items) == _PAGE_SIZE + 1
    assert cursor == datetime.fromtimestamp(1782841200, tz=UTC).isoformat()  # newest end_time
    # second page resumes from the first page's end_time
    assert client.get.call_args_list[1].kwargs["params"]["start_time"] == 1782837600


@pytest.mark.asyncio
async def test_fetch_since_honors_retry_after_then_succeeds() -> None:
    page = {"tickets": [], "end_time": 1782837600, "end_of_stream": True}
    patcher, client = _client_returning(
        _resp(429, headers={"Retry-After": "1"}), _resp(200, page)
    )
    with patcher, patch("app.integrations.zendesk.asyncio.sleep", AsyncMock()) as sleep:
        await ZendeskIntegration().fetch_since(
            "tok", ChannelRef(external_id="acme", name="ws"), None
        )

    sleep.assert_awaited_once_with(1)
    assert client.get.call_count == 2


@pytest.mark.asyncio
async def test_fetch_since_boundary_record_returns_and_holds_cursor() -> None:
    """Zendesk's incremental export is inclusive of start_time, so the boundary ticket
    re-appears with end_time == start_time. We return it (downstream source_events dedup
    handles the duplicate) and hold the cursor rather than trying to skip it. Verified
    live against a real Zendesk trial."""
    cur = "2026-07-02T14:19:16+00:00"
    start = int(datetime.fromisoformat(cur).timestamp())
    page = {
        "tickets": [{"id": 1, "subject": "x", "updated_at": "2026-07-02T14:19:15Z"}],
        "end_time": start,  # end_time == start_time: no forward progress this sweep
        "end_of_stream": True,
    }
    patcher, _ = _client_returning(_resp(200, page))
    with patcher:
        items, cursor = await ZendeskIntegration().fetch_since(
            "tok", ChannelRef(external_id="acme", name="ws"), cur
        )
    assert len(items) == 1  # boundary ticket returned (deduped downstream)
    assert cursor == cur  # cursor held, not advanced past unfetched data


# ── source_sync contract (the boundary Slack's review bugs lived at) ────────────


@pytest.mark.asyncio
async def test_fetch_since_cursor_is_source_sync_parseable() -> None:
    """The returned cursor must be ISO-8601: source_sync stores it via last_synced_at
    and re-parses it with _parse_iso (datetime.fromisoformat). A non-ISO cursor wedges
    the sync (this is exactly Slack review bug #1)."""
    from app.jobs.tasks.source_sync import _parse_iso

    page = {
        "tickets": [{"id": 1, "subject": "a", "updated_at": "2026-06-30T10:00:00Z"}],
        "end_time": 1782837600,
        "end_of_stream": True,
    }
    patcher, _ = _client_returning(_resp(200, page))
    with patcher:
        _, cursor = await ZendeskIntegration().fetch_since(
            "tok", ChannelRef(external_id="acme", name="ws"), None
        )
    assert _parse_iso(cursor) is not None  # does not raise -> source_sync can advance


@pytest.mark.asyncio
async def test_fetch_since_auth_error_surfaces_as_httpstatuserror() -> None:
    """A 401 must surface as httpx.HTTPStatusError so source_sync classifies it as
    auth_broken (re-auth needed) — not a custom exception it ignores (Slack bug #3)."""
    req = httpx.Request("GET", "https://acme.zendesk.com/api/v2/incremental/tickets.json")
    mock_client = AsyncMock()
    mock_client.get.return_value = httpx.Response(401, request=req)
    with patch("app.integrations.zendesk.http_client", return_value=mock_client), pytest.raises(
        httpx.HTTPStatusError
    ) as exc:
        await ZendeskIntegration().fetch_since(
            "tok", ChannelRef(external_id="acme", name="ws"), None
        )
    assert exc.value.response.status_code == 401


# ── webhook verification (base64 HMAC over timestamp + body) ────────────────────


def _sign(secret: str, timestamp: str, body: bytes) -> str:
    return base64.b64encode(
        hmac.new(secret.encode(), timestamp.encode() + body, hashlib.sha256).digest()
    ).decode()


def test_verify_webhook_accepts_valid_signature() -> None:
    secret, ts, body = "shhh", str(int(time.time())), b'{"ticket":{}}'
    headers = {
        "x-zendesk-webhook-signature": _sign(secret, ts, body),
        "x-zendesk-webhook-signature-timestamp": ts,
    }
    assert ZendeskIntegration().verify_webhook(headers, body, secret) is True


def test_verify_webhook_rejects_tampered_body() -> None:
    secret, ts, body = "shhh", str(int(time.time())), b'{"ticket":{}}'
    headers = {
        "x-zendesk-webhook-signature": _sign(secret, ts, body),
        "x-zendesk-webhook-signature-timestamp": ts,
    }
    assert ZendeskIntegration().verify_webhook(headers, b'{"tampered":1}', secret) is False


def test_verify_webhook_rejects_empty_secret() -> None:
    headers = {"x-zendesk-webhook-signature": "abc", "x-zendesk-webhook-signature-timestamp": "1"}
    assert ZendeskIntegration().verify_webhook(headers, b"{}", "") is False


def test_verify_webhook_case_insensitive_headers() -> None:
    secret, ts, body = "shhh", str(int(time.time())), b"{}"
    headers = {
        "X-Zendesk-Webhook-Signature": _sign(secret, ts, body),
        "X-Zendesk-Webhook-Signature-Timestamp": ts,
    }
    assert ZendeskIntegration().verify_webhook(headers, body, secret) is True


# ── normalize ──────────────────────────────────────────────────────────────────


def test_normalize_ticket() -> None:
    ticket = {
        "id": 42,
        "subject": "Refund request",
        "description": "Customer wants a refund",
        "requester_id": 777,
        "updated_at": "2026-06-30T09:30:00Z",
        "_subdomain": "acme",
    }
    event = ZendeskIntegration().normalize(RawItem(external_id="42", payload=ticket))
    assert event.provider == "zendesk"
    assert event.event_type == "ticket"
    assert event.source_id == "42"
    assert event.external_event_id == "zendesk:42:2026-06-30T09:30:00Z"
    assert event.actor["id"] == "777"
    assert event.content == "Refund request\n\nCustomer wants a refund"
    assert event.url == "https://acme.zendesk.com/agent/tickets/42"


def test_normalize_rejects_non_ticket() -> None:
    with pytest.raises(ValueError):
        ZendeskIntegration().normalize(RawItem(external_id="x", payload={"no": "id"}))


def test_parse_ts_treats_naive_cursor_as_utc() -> None:
    # The fetch_since cursor path calls _parse_ts(...).timestamp(); a naive cursor must
    # be treated as UTC so start_time doesn't drift with the host timezone.
    from app.integrations.zendesk import _parse_ts

    assert _parse_ts("2026-07-02T14:19:16").timestamp() == (
        _parse_ts("2026-07-02T14:19:16+00:00").timestamp()
    )
    assert _parse_ts(None) is None


# ── conformance ────────────────────────────────────────────────────────────────


def test_zendesk_integration_implements_protocol() -> None:
    integration = ZendeskIntegration()
    assert isinstance(integration, SourceIntegration)
    assert integration.provider == "zendesk"
