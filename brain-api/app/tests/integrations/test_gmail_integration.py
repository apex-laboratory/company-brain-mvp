"""Gmail integration unit tests (KAN-2).

Outbound HTTP is intercepted with ``httpx.MockTransport``; OAuth reads flow through
``google_common`` whose ``settings`` reference is stubbed.
"""
from __future__ import annotations

import base64
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.integrations import base
from app.integrations import google_common
from app.integrations.gmail import GmailIntegration

_GOOGLE_SETTINGS = SimpleNamespace(
    google_client_id="client-123",
    google_client_secret="secret-abc",
    google_pubsub_topic="projects/p/topics/t",
    google_pubsub_verification_token="tok",
)


@pytest.fixture
def gmail(monkeypatch: pytest.MonkeyPatch) -> GmailIntegration:
    monkeypatch.setattr(google_common, "settings", _GOOGLE_SETTINGS)
    return GmailIntegration()


def _install_transport(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(base, "_client", client)


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def _message(mid: str, subject: str, body: str) -> dict:
    return {
        "id": mid,
        "internalDate": "1718000000000",
        "payload": {
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "From", "value": "sender@acme.com"},
            ],
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64(body)}},
            ],
        },
    }


def test_authorize_url_has_gmail_scope(gmail: GmailIntegration) -> None:
    url = gmail.authorize_url("STATE1", "https://api.example.com/cb")
    qs = parse_qs(urlparse(url).query)
    assert qs["access_type"] == ["offline"]
    assert "gmail.readonly" in qs["scope"][0]


async def test_fetch_since_bootstrap_reads_profile_history_id(
    gmail: GmailIntegration, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/profile"):
            return httpx.Response(200, json={"historyId": "5000"})
        if path.endswith("/messages"):  # messages.list
            assert request.url.params.get("q", "").startswith("after:")
            return httpx.Response(200, json={"messages": [{"id": "m1"}]})
        if path.endswith("/messages/m1"):
            return httpx.Response(200, json=_message("m1", "Hi", "hello body"))
        raise AssertionError(f"unexpected path {path}")

    _install_transport(monkeypatch, handler)
    channel = base.ChannelRef(external_id="ada@acme.com", name="workspace")
    items, cursor = await gmail.fetch_since("tok", channel, None)
    assert [i.external_id for i in items] == ["m1"]
    assert cursor == "5000"  # opaque historyId becomes the next cursor


async def test_fetch_since_incremental_uses_history(
    gmail: GmailIntegration, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/history"):
            assert request.url.params.get("startHistoryId") == "5000"
            return httpx.Response(
                200,
                json={
                    "history": [{"messagesAdded": [{"message": {"id": "m2"}}]}],
                    "historyId": "5100",
                },
            )
        if path.endswith("/messages/m2"):
            return httpx.Response(200, json=_message("m2", "New", "new body"))
        raise AssertionError(f"unexpected path {path}")

    _install_transport(monkeypatch, handler)
    channel = base.ChannelRef(external_id="ada@acme.com", name="workspace")
    items, cursor = await gmail.fetch_since("tok", channel, "5000")
    assert [i.external_id for i in items] == ["m2"]
    assert cursor == "5100"


async def test_fetch_since_falls_back_to_full_sync_on_404(
    gmail: GmailIntegration, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/history"):
            return httpx.Response(404, json={"error": {"code": 404}})
        if path.endswith("/profile"):
            return httpx.Response(200, json={"historyId": "9000"})
        if path.endswith("/messages"):
            return httpx.Response(200, json={"messages": [{"id": "m9"}]})
        if path.endswith("/messages/m9"):
            return httpx.Response(200, json=_message("m9", "Recovered", "body"))
        raise AssertionError(f"unexpected path {path}")

    _install_transport(monkeypatch, handler)
    channel = base.ChannelRef(external_id="ada@acme.com", name="workspace")
    items, cursor = await gmail.fetch_since("tok", channel, "1")  # stale historyId
    assert [i.external_id for i in items] == ["m9"]
    assert cursor == "9000"  # re-established from getProfile


def test_normalize_extracts_subject_and_body(gmail: GmailIntegration) -> None:
    event = gmail.normalize(base.RawItem(external_id="m1", payload=_message("m1", "Hi", "hello body")))
    assert event.provider == "gmail"
    assert event.source_id == "m1"
    assert event.event_type == "email"
    assert event.external_event_id == "gmail:m1"
    assert "Hi" in event.content
    assert "hello body" in event.content
    assert event.actor["email"] == "sender@acme.com"
    assert event.url.endswith("#all/m1")


def test_verify_webhook_is_false(gmail: GmailIntegration) -> None:
    # Gmail push is verified by the Pub/Sub URL token in the webhooks service.
    assert gmail.verify_webhook({}, b"", "anything") is False
