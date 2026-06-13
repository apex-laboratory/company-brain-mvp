"""Notion integration unit tests (KAN-2).

Outbound HTTP is intercepted with ``httpx.MockTransport`` swapped into the shared
client — no network, no new test dependency.
"""
from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.integrations import base
from app.integrations.notion import NotionIntegration


@pytest.fixture
def notion() -> NotionIntegration:
    return NotionIntegration()


def _install_transport(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(base, "_client", client)


def test_authorize_url_carries_state_and_redirect(notion: NotionIntegration) -> None:
    url = notion.authorize_url("STATE123", "https://api.example.com/cb")
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    assert parsed.netloc == "api.notion.com"
    assert qs["state"] == ["STATE123"]
    assert qs["redirect_uri"] == ["https://api.example.com/cb"]
    assert qs["response_type"] == ["code"]


async def test_exchange_code_returns_tokens(
    notion: NotionIntegration, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/oauth/token")
        assert request.headers["Authorization"].startswith("Basic ")
        return httpx.Response(
            200,
            json={
                "access_token": "secret-token",
                "workspace_id": "ws-123",
                "workspace_name": "Acme HQ",
            },
        )

    _install_transport(monkeypatch, handler)
    tokens = await notion.exchange_code("the-code", "https://api.example.com/cb")
    assert tokens.access_token == "secret-token"
    assert tokens.external_account_id == "ws-123"
    assert tokens.refresh_token is None
    assert tokens.expires_at is None
    assert tokens.raw["workspace_name"] == "Acme HQ"


def _page(page_id: str, edited: str, title: str) -> dict:
    return {
        "object": "page",
        "id": page_id,
        "last_edited_time": edited,
        "url": f"https://notion.so/{page_id}",
        "last_edited_by": {"id": "user-1"},
        "properties": {
            "Name": {"type": "title", "title": [{"plain_text": title}]},
        },
    }


async def test_fetch_since_filters_by_cursor_and_advances(
    notion: NotionIntegration, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Newest-first, as Notion returns when sorted descending by last_edited_time.
    results = [
        _page("p3", "2026-06-13T10:00:00.000Z", "Newest"),
        _page("p2", "2026-06-12T10:00:00.000Z", "Middle"),
        _page("p1", "2026-06-11T10:00:00.000Z", "Oldest"),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": results, "has_more": False})

    _install_transport(monkeypatch, handler)
    channel = base.ChannelRef(external_id="ws", name="workspace")

    # Cursor at p2's time → only p3 is newer.
    items, next_cursor = await notion.fetch_since(
        "tok", channel, "2026-06-12T10:00:00.000Z"
    )
    assert [i.external_id for i in items] == ["p3"]
    assert next_cursor == "2026-06-13T10:00:00+00:00"

    # No cursor → everything, cursor advances to the newest seen.
    items, next_cursor = await notion.fetch_since("tok", channel, None)
    assert {i.external_id for i in items} == {"p1", "p2", "p3"}
    assert next_cursor == "2026-06-13T10:00:00+00:00"


def test_normalize_maps_page_to_canonical_event(notion: NotionIntegration) -> None:
    item = base.RawItem(
        external_id="p1", payload=_page("p1", "2026-06-13T10:00:00.000Z", "Hello")
    )
    event = notion.normalize(item)
    assert event.provider == "notion"
    assert event.source_id == "p1"
    assert event.event_type == "page"
    assert event.external_event_id == "p1:2026-06-13T10:00:00.000Z"
    assert event.content == "Hello"
    assert event.url == "https://notion.so/p1"
    assert event.actor["id"] == "user-1"


def test_verify_webhook_is_always_false(notion: NotionIntegration) -> None:
    assert notion.verify_webhook({}, b"{}", "secret") is False
