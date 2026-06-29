"""Unit tests for the source OAuth integration.

Covers the data-driven provider registry: catalog shape, authorize-URL building
with configured credentials, the 501 for an unconfigured provider, and the
normalized token parse for the two auth styles (Slack ok=false handling, Notion
account extraction). Network calls are stubbed — no real provider is hit.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.integrations import source_oauth
from app.integrations.source_oauth import SourceOAuthError


def _set_creds(monkeypatch: pytest.MonkeyPatch, **creds: str) -> None:
    """Replace the module's frozen settings with a namespace of test creds.

    The real ``settings`` object is frozen, so tests swap ``source_oauth.settings``
    for a stand-in carrying just the provider credential fields the registry reads.
    """
    base = {
        "slack_client_id": "",
        "slack_client_secret": "",
        "notion_client_id": "",
        "notion_client_secret": "",
        "github_client_id": "",
        "github_client_secret": "",
        "jira_client_id": "",
        "jira_client_secret": "",
        "zendesk_client_id": "",
        "zendesk_client_secret": "",
        "zendesk_subdomain": "",
    }
    base.update(creds)
    monkeypatch.setattr(source_oauth, "settings", SimpleNamespace(**base))


def test_catalog_lists_all_five_read_only_providers() -> None:
    catalog = source_oauth.provider_catalog()
    assert {p["provider"] for p in catalog} == {
        "slack",
        "notion",
        "github",
        "jira",
        "zendesk",
    }
    assert all(p["readOnly"] is True for p in catalog)


def test_build_authorize_url_includes_scopes_and_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_creds(monkeypatch, slack_client_id="cid", slack_client_secret="sec")

    url = source_oauth.build_authorize_url(
        provider="slack",
        redirect_uri="https://app.example/cb",
        state="st_abc",
        scopes=["channels:read", "channels:history"],
    )
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    assert parsed.netloc == "slack.com"
    assert qs["client_id"] == ["cid"]
    assert qs["state"] == ["st_abc"]
    assert qs["redirect_uri"] == ["https://app.example/cb"]
    assert qs["scope"] == ["channels:read channels:history"]


def test_build_authorize_url_unconfigured_provider_raises_501(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_creds(monkeypatch)  # all providers unconfigured

    with pytest.raises(SourceOAuthError) as exc:
        source_oauth.build_authorize_url(
            provider="notion", redirect_uri="x", state="s", scopes=[]
        )
    assert exc.value.status == 501


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeClient:
    def __init__(self, payload: dict[str, Any], capture: dict[str, Any]) -> None:
        self._payload = payload
        self._capture = capture

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        self._capture["url"] = url
        self._capture["kwargs"] = kwargs
        return _FakeResponse(self._payload)


@pytest.mark.asyncio
async def test_exchange_code_slack_parses_team_and_scopes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_creds(monkeypatch, slack_client_id="cid", slack_client_secret="sec")
    capture: dict[str, Any] = {}
    payload = {
        "ok": True,
        "access_token": "xoxb-token",
        "scope": "channels:read,channels:history",
        "team": {"id": "T123", "name": "Riverline"},
    }
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kw: _FakeClient(payload, capture))

    token = await source_oauth.exchange_code(
        provider="slack", code="c", redirect_uri="https://cb", scopes=["channels:read"]
    )
    assert token.access_token == "xoxb-token"
    assert token.external_account_id == "T123"
    assert token.account_name == "Riverline"
    assert token.scopes == ["channels:read", "channels:history"]
    # Slack uses the form auth style: client creds in the body, not Basic auth.
    assert "data" in capture["kwargs"] and "auth" not in capture["kwargs"]


@pytest.mark.asyncio
async def test_exchange_code_slack_ok_false_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_creds(monkeypatch, slack_client_id="cid", slack_client_secret="sec")
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **_kw: _FakeClient({"ok": False, "error": "bad_code"}, {}),
    )

    with pytest.raises(SourceOAuthError):
        await source_oauth.exchange_code(
            provider="slack", code="c", redirect_uri="https://cb", scopes=[]
        )


@pytest.mark.asyncio
async def test_exchange_code_notion_uses_basic_auth_and_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_creds(monkeypatch, notion_client_id="cid", notion_client_secret="sec")
    capture: dict[str, Any] = {}
    payload = {
        "access_token": "secret_n",
        "workspace_id": "ws_1",
        "workspace_name": "Docs",
    }
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kw: _FakeClient(payload, capture))

    token = await source_oauth.exchange_code(
        provider="notion", code="c", redirect_uri="https://cb", scopes=[]
    )
    assert token.external_account_id == "ws_1"
    assert token.account_name == "Docs"
    # Notion uses json_basic: JSON body + HTTP Basic client auth.
    assert "json" in capture["kwargs"] and "auth" in capture["kwargs"]
