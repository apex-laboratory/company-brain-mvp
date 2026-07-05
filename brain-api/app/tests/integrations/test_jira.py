"""Unit tests for the Jira Cloud integration (OAuth 3LO + polling).

Provider HTTP calls are mocked; no real Atlassian API calls are made. The connector
programs against the SourceIntegration protocol, so these tests pass tokens as plain
strings (exactly how the sources service and sync jobs call it), not OAuthTokens.
"""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.config.settings import settings
from app.integrations.base import ChannelRef, ConnectorAuthError, RawItem, SourceIntegration
from app.integrations.jira import (
    JiraIntegration,
    _adf_to_text,
    _build_jql,
    _group_by_site,
    _parse_ts,
)


def _resp(status: int = 200, json_payload: object = None, headers: dict | None = None):
    """Build one mock httpx response; ``raise_for_status`` raises on status >= 400."""
    r = MagicMock()
    r.status_code = status
    r.headers = headers or {}
    r.json.return_value = {} if json_payload is None else json_payload
    if status >= 400:
        request = httpx.Request("GET", "https://api.atlassian.com")
        response = httpx.Response(status, request=request)
        r.raise_for_status.side_effect = httpx.HTTPStatusError(
            "err", request=request, response=response
        )
    else:
        r.raise_for_status.return_value = None
    return r


def _mock_client(get=None, post=None):
    """AsyncMock http client with sequenced ``get``/``post`` responses."""
    client = AsyncMock()
    if get is not None:
        client.get.side_effect = list(get)
    if post is not None:
        client.post.side_effect = list(post)
    return patch("app.integrations.jira.http_client", return_value=client), client


_SITE = {"id": "cloud-1", "name": "Acme", "url": "https://acme.atlassian.net",
         "scopes": ["read:jira-work"]}


# ── protocol ────────────────────────────────────────────────────────────────────


def test_satisfies_source_integration_protocol() -> None:
    assert isinstance(JiraIntegration(), SourceIntegration)


# ── OAuth ─────────────────────────────────────────────────────────────────────


def test_authorize_url_contains_required_3lo_params() -> None:
    url = JiraIntegration().authorize_url("test-state", "https://x/cb")
    assert url.startswith("https://auth.atlassian.com/authorize?")
    assert "audience=api.atlassian.com" in url
    assert f"client_id={settings.jira_client_id}" in url
    assert "state=test-state" in url
    assert "response_type=code" in url
    assert "prompt=consent" in url
    assert "offline_access" in url  # required for a refresh token


@pytest.mark.asyncio
async def test_exchange_code_resolves_cloudid_and_refresh_token() -> None:
    token_resp = _resp(200, {
        "access_token": "at-123",
        "refresh_token": "rt-abc",
        "expires_in": 3600,
        "scope": "read:jira-work offline_access",
    })
    resources_resp = _resp(200, [_SITE])
    patcher, _ = _mock_client(get=[resources_resp], post=[token_resp])
    with patcher:
        tokens = await JiraIntegration().exchange_code("code-1", "https://x/cb")

    assert tokens.access_token == "at-123"
    assert tokens.refresh_token == "rt-abc"
    assert tokens.external_account_id == "cloud-1"  # first Jira site's cloudId
    assert tokens.expires_at is not None and tokens.expires_at > datetime.now(UTC)
    assert tokens.raw["workspace_name"] == "Acme"  # connection label
    assert "read:jira-work" in tokens.scopes


@pytest.mark.asyncio
async def test_exchange_code_ignores_confluence_only_sites() -> None:
    conf_site = {"id": "conf-1", "name": "Docs", "url": "https://x",
                 "scopes": ["read:confluence-content.summary"]}
    token_resp = _resp(200, {"access_token": "at", "refresh_token": "rt", "expires_in": 3600})
    patcher, _ = _mock_client(get=[_resp(200, [conf_site, _SITE])], post=[token_resp])
    with patcher:
        tokens = await JiraIntegration().exchange_code("c", "https://x/cb")
    assert tokens.external_account_id == "cloud-1"  # skipped the Confluence-only site


@pytest.mark.asyncio
async def test_refresh_rotates_refresh_token() -> None:
    refreshed = _resp(200, {
        "access_token": "at-new",
        "refresh_token": "rt-new",  # rotated — must be surfaced
        "expires_in": 3600,
        "scope": "read:jira-work offline_access",
    })
    patcher, _ = _mock_client(post=[refreshed])
    with patcher:
        tokens = await JiraIntegration().refresh("rt-old")
    assert tokens.access_token == "at-new"
    assert tokens.refresh_token == "rt-new"
    assert tokens.expires_at is not None


# ── list_channels (projects flattened across sites) ─────────────────────────────


@pytest.mark.asyncio
async def test_list_channels_flattens_projects_across_sites() -> None:
    resources = _resp(200, [_SITE])
    projects_p1 = _resp(200, {
        "values": [{"id": "10001", "key": "ENG", "name": "Engineering"}],
        "isLast": False, "maxResults": 1,
    })
    projects_p2 = _resp(200, {
        "values": [{"id": "10002", "key": "OPS", "name": "Operations"}],
        "isLast": True, "maxResults": 1,
    })
    patcher, _ = _mock_client(get=[resources, projects_p1, projects_p2])
    with patcher:
        channels = await JiraIntegration().list_channels("at")

    assert [c.external_id for c in channels] == ["cloud-1:10001", "cloud-1:10002"]
    assert channels[0].name == "Acme / Engineering"


# ── fetch_since ────────────────────────────────────────────────────────────────


def _issue(issue_id: str, key: str, updated: str) -> dict:
    return {
        "id": issue_id,
        "key": key,
        "fields": {
            "summary": f"Issue {key}",
            "updated": updated,
            "created": "2026-07-01T09:00:00.000+0000",
            "creator": {"accountId": "acc-1", "displayName": "Dev One"},
        },
    }


@pytest.mark.asyncio
async def test_fetch_since_cursor_roundtrips_with_source_sync() -> None:
    """Incoming ISO cursor builds a JQL bound; returned cursor is ISO-8601 (the newest
    `updated`) so source_sync's _parse_iso re-parses + stores it — the boundary that
    produced Slack's review bugs."""
    from app.jobs.tasks.source_sync import _parse_iso

    resources = _resp(200, [_SITE])
    myself = _resp(200, {"timeZone": "UTC"})
    search = _resp(200, {
        "issues": [_issue("1", "ENG-1", "2026-07-02T14:19:16.000+0000")],
        "isLast": True,
    })
    patcher, client = _mock_client(get=[resources, myself], post=[search])
    incoming = "2026-07-02T10:00:00+00:00"
    with patcher:
        items, cursor = await JiraIntegration().fetch_since(
            "at", ChannelRef(external_id="cloud-1", name="ws"), incoming
        )

    assert len(items) == 1
    # JQL carries a minute-floored updated bound in the (UTC) user timezone
    jql = client.post.call_args.kwargs["json"]["jql"]
    assert 'updated >= "2026-07-02 10:00"' in jql
    assert jql.endswith("ORDER BY updated ASC")
    # returned cursor is the newest updated, ISO-8601, and does NOT raise in the parser
    assert cursor == "2026-07-02T14:19:16+00:00"
    assert _parse_iso(cursor) is not None


@pytest.mark.asyncio
async def test_fetch_since_follows_next_page_token_without_islast() -> None:
    """A page that returns a nextPageToken but omits isLast must NOT stop pagination —
    nextPageToken absence is the only authoritative last-page signal."""
    resources = _resp(200, [_SITE])
    myself = _resp(200, {"timeZone": "UTC"})
    page1 = _resp(200, {  # no isLast field, has nextPageToken -> must continue
        "issues": [_issue("1", "ENG-1", "2026-07-02T14:00:00.000+0000")],
        "nextPageToken": "tok-2",
    })
    page2 = _resp(200, {  # no nextPageToken -> last page
        "issues": [_issue("2", "ENG-2", "2026-07-02T15:00:00.000+0000")],
    })
    patcher, client = _mock_client(get=[resources, myself], post=[page1, page2])
    with patcher:
        items, cursor = await JiraIntegration().fetch_since(
            "at", ChannelRef(external_id="cloud-1", name="ws"), "2026-07-02T10:00:00+00:00"
        )
    assert [i.external_id for i in items] == ["1", "2"]  # both pages collected
    assert client.post.call_count == 2
    assert client.post.call_args.kwargs["json"]["nextPageToken"] == "tok-2"
    assert cursor == "2026-07-02T15:00:00+00:00"  # newest across both pages


@pytest.mark.asyncio
async def test_fetch_since_holds_cursor_when_nothing_new() -> None:
    resources = _resp(200, [_SITE])
    myself = _resp(200, {"timeZone": "UTC"})
    search = _resp(200, {"issues": [], "isLast": True})
    patcher, _ = _mock_client(get=[resources, myself], post=[search])
    incoming = "2026-07-02T10:00:00+00:00"
    with patcher:
        items, cursor = await JiraIntegration().fetch_since(
            "at", ChannelRef(external_id="cloud-1", name="ws"), incoming
        )
    assert items == []
    assert cursor == incoming  # unchanged — nothing newer to advance to


@pytest.mark.asyncio
async def test_fetch_since_filters_selected_projects_by_site() -> None:
    resources = _resp(200, [_SITE])
    myself = _resp(200, {"timeZone": "UTC"})
    search = _resp(200, {"issues": [], "isLast": True})
    patcher, client = _mock_client(get=[resources, myself], post=[search])
    with patcher:
        await JiraIntegration().fetch_since(
            "at", ChannelRef(external_id="cloud-1", name="ws"),
            "2026-07-02T10:00:00+00:00",
            allowed_channels={"cloud-1:10001", "cloud-1:10002"},
        )
    jql = client.post.call_args.kwargs["json"]["jql"]
    assert "project in (10001,10002)" in jql


@pytest.mark.asyncio
async def test_fetch_since_skips_site_with_no_selected_project() -> None:
    """A selection that names no project on this site short-circuits before any HTTP call
    into the site (only accessible-resources runs)."""
    resources = _resp(200, [_SITE])
    patcher, client = _mock_client(get=[resources])
    with patcher:
        items, cursor = await JiraIntegration().fetch_since(
            "at", ChannelRef(external_id="cloud-1", name="ws"),
            "2026-07-02T10:00:00+00:00",
            allowed_channels={"other-cloud:999"},
        )
    assert items == [] and cursor == "2026-07-02T10:00:00+00:00"
    client.post.assert_not_called()  # never searched the site


@pytest.mark.asyncio
async def test_fetch_since_401_raises_connector_auth_error() -> None:
    """A 401 must surface as ConnectorAuthError so source_sync marks the connection
    auth_broken (a plain error would leave it unflagged)."""
    resources = _resp(200, [_SITE])
    myself = _resp(200, {"timeZone": "UTC"})
    search_401 = _resp(401)
    patcher, _ = _mock_client(get=[resources, myself], post=[search_401])
    with patcher, pytest.raises(ConnectorAuthError):
        await JiraIntegration().fetch_since(
            "at", ChannelRef(external_id="cloud-1", name="ws"), "2026-07-02T10:00:00+00:00"
        )


@pytest.mark.asyncio
async def test_fetch_since_holds_cursor_on_transient_5xx() -> None:
    resources = _resp(200, [_SITE])
    myself = _resp(200, {"timeZone": "UTC"})
    patcher, _ = _mock_client(get=[resources, myself], post=[_resp(503)])
    incoming = "2026-07-02T10:00:00+00:00"
    with patcher:
        items, cursor = await JiraIntegration().fetch_since(
            "at", ChannelRef(external_id="cloud-1", name="ws"), incoming
        )
    assert items == [] and cursor == incoming  # held for retry, not advanced


# ── normalize ──────────────────────────────────────────────────────────────────


def test_normalize_issue_flattens_adf_and_builds_browse_url() -> None:
    issue = _issue("1001", "ENG-42", "2026-07-02T14:19:16.000+0000")
    issue["fields"]["description"] = {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "First line."}]},
            {"type": "paragraph", "content": [{"type": "text", "text": "Second line."}]},
        ],
    }
    issue["_site_url"] = "https://acme.atlassian.net/"
    event = JiraIntegration().normalize(RawItem(external_id="1001", payload=issue))

    assert event.provider == "jira"
    assert event.event_type == "issue"
    assert event.source_id == "1001"
    assert event.external_event_id == "jira:1001:2026-07-02T14:19:16.000+0000"
    assert "Issue ENG-42" in event.content
    assert "First line." in event.content and "Second line." in event.content
    assert event.url == "https://acme.atlassian.net/browse/ENG-42"
    assert event.actor == {"id": "acc-1", "email": "", "name": "Dev One"}
    assert event.created_at.tzinfo is not None


def test_normalize_handles_missing_description() -> None:
    issue = _issue("2", "ENG-2", "2026-07-02T14:19:16.000+0000")
    event = JiraIntegration().normalize(RawItem(external_id="2", payload=issue))
    assert event.content == "Issue ENG-2"


# ── webhooks ───────────────────────────────────────────────────────────────────


def test_verify_webhook_false_polling_only() -> None:
    assert JiraIntegration().verify_webhook({}, b"{}", "secret") is False


# ── helpers ────────────────────────────────────────────────────────────────────


def test_build_jql_minute_floors_in_timezone() -> None:
    since = datetime(2026, 7, 2, 14, 19, 45, tzinfo=UTC)  # 14:19:45 UTC
    jql = _build_jql(since, ZoneInfo("America/New_York"), None)
    # UTC 14:19 -> 10:19 EDT, floored to the minute
    assert 'updated >= "2026-07-02 10:19"' in jql


def test_build_jql_no_cursor_is_order_only() -> None:
    assert _build_jql(None, ZoneInfo("UTC"), None) == "ORDER BY updated ASC"


def test_group_by_site_partitions_selection() -> None:
    grouped = _group_by_site({"c1:10", "c1:11", "c2:20"})
    assert grouped == {"c1": {"10", "11"}, "c2": {"20"}}
    assert _group_by_site(None) is None


def test_adf_to_text_walks_unknown_nodes() -> None:
    doc = {"type": "doc", "content": [
        {"type": "panel", "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "inside panel"}]},
        ]},
    ]}
    assert "inside panel" in _adf_to_text(doc)


def test_parse_ts_treats_naive_as_utc() -> None:
    assert _parse_ts("2026-07-02T14:19:16.000") == datetime(2026, 7, 2, 14, 19, 16, tzinfo=UTC)
    assert _parse_ts(None) is None
