"""Google Drive integration unit tests (KAN-2).

Outbound HTTP is intercepted with ``httpx.MockTransport`` swapped into the shared
client — no network. OAuth reads flow through ``google_common``, so its ``settings``
reference is stubbed.
"""
from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.integrations import base
from app.integrations import google_common
from app.integrations.google_drive import GoogleDriveIntegration

_GOOGLE_SETTINGS = SimpleNamespace(
    google_client_id="client-123",
    google_client_secret="secret-abc",
    google_pubsub_topic="projects/p/topics/t",
    google_pubsub_verification_token="tok",
)


@pytest.fixture
def drive(monkeypatch: pytest.MonkeyPatch) -> GoogleDriveIntegration:
    monkeypatch.setattr(google_common, "settings", _GOOGLE_SETTINGS)
    return GoogleDriveIntegration()


def _install_transport(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(base, "_client", client)


def _file(fid: str, name: str, mime: str, modified: str) -> dict:
    return {
        "id": fid,
        "name": name,
        "mimeType": mime,
        "modifiedTime": modified,
        "webViewLink": f"https://drive.google.com/file/{fid}",
        "owners": [{"displayName": "Ada", "emailAddress": "ada@acme.com"}],
    }


def test_authorize_url_has_offline_consent_and_scope(drive: GoogleDriveIntegration) -> None:
    url = drive.authorize_url("STATE1", "https://api.example.com/cb")
    qs = parse_qs(urlparse(url).query)
    assert qs["client_id"] == ["client-123"]
    assert qs["access_type"] == ["offline"]
    assert qs["prompt"] == ["consent"]
    assert qs["state"] == ["STATE1"]
    assert "drive.readonly" in qs["scope"][0]


async def test_exchange_code_returns_tokens_with_email_account(
    drive: GoogleDriveIntegration, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(
                200,
                json={
                    "access_token": "at-1",
                    "refresh_token": "rt-1",
                    "expires_in": 3600,
                    "scope": "https://www.googleapis.com/auth/drive.readonly",
                },
            )
        assert request.url.path.endswith("/userinfo")
        return httpx.Response(200, json={"email": "ada@acme.com"})

    _install_transport(monkeypatch, handler)
    tokens = await drive.exchange_code("code", "cb")
    assert tokens.access_token == "at-1"
    assert tokens.refresh_token == "rt-1"
    assert tokens.expires_at is not None
    assert tokens.external_account_id == "ada@acme.com"


async def test_fetch_since_bootstrap_backfills_and_returns_start_token(
    drive: GoogleDriveIntegration, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc = _file("f1", "Doc", "application/vnd.google-apps.document", "2026-06-10T10:00:00Z")

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/drive/v3/changes/startPageToken":
            return httpx.Response(200, json={"startPageToken": "SP1"})
        if path == "/drive/v3/files":  # files.list backfill
            return httpx.Response(200, json={"files": [doc]})
        if path == "/drive/v3/files/f1/export":  # Google Doc → plain text
            assert request.url.params.get("mimeType") == "text/plain"
            return httpx.Response(200, text="exported body")
        raise AssertionError(f"unexpected path {path}")

    _install_transport(monkeypatch, handler)
    channel = base.ChannelRef(external_id="ada@acme.com", name="workspace")
    items, cursor = await drive.fetch_since("tok", channel, None)
    assert [i.external_id for i in items] == ["f1"]
    assert items[0].payload["_content"] == "exported body"
    assert cursor == "SP1"  # opaque start page token becomes the next cursor


async def test_fetch_since_incremental_uses_changes_feed(
    drive: GoogleDriveIntegration, monkeypatch: pytest.MonkeyPatch
) -> None:
    changed = _file("f2", "notes.txt", "text/plain", "2026-06-12T10:00:00Z")

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/drive/v3/changes":
            assert request.url.params.get("pageToken") == "SP1"
            return httpx.Response(
                200,
                json={
                    "changes": [{"removed": False, "fileId": "f2", "file": changed}],
                    "newStartPageToken": "SP2",
                },
            )
        if path == "/drive/v3/files/f2":  # text/* → media download
            assert request.url.params.get("alt") == "media"
            return httpx.Response(200, text="plain text body")
        raise AssertionError(f"unexpected path {path}")

    _install_transport(monkeypatch, handler)
    channel = base.ChannelRef(external_id="ada@acme.com", name="workspace")
    items, cursor = await drive.fetch_since("tok", channel, "SP1")
    assert [i.external_id for i in items] == ["f2"]
    assert cursor == "SP2"


def test_normalize_maps_file(drive: GoogleDriveIntegration) -> None:
    payload = {**_file("f1", "Doc", "text/plain", "2026-06-10T10:00:00Z"), "_content": "body"}
    event = drive.normalize(base.RawItem(external_id="f1", payload=payload))
    assert event.provider == "google_drive"
    assert event.source_id == "f1"
    assert event.event_type == "file"
    assert event.external_event_id == "f1:2026-06-10T10:00:00Z"
    assert event.content.startswith("Doc")
    assert "body" in event.content
    assert event.actor["email"] == "ada@acme.com"
    assert event.url.endswith("/f1")


def test_verify_webhook_checks_channel_token(drive: GoogleDriveIntegration) -> None:
    assert drive.verify_webhook({"X-Goog-Channel-Token": "s3cret"}, b"", "s3cret") is True
    assert drive.verify_webhook({"X-Goog-Channel-Token": "wrong"}, b"", "s3cret") is False
    assert drive.verify_webhook({}, b"", "s3cret") is False
    assert drive.verify_webhook({"X-Goog-Channel-Token": "x"}, b"", "") is False
