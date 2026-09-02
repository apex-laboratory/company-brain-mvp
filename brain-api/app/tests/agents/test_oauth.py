"""The shared OAuth flow (agent-builder-plan §5.3, phase 2).

Two properties are worth more than the rest here, so they are tested first and
most:

* **A token never reaches a log line or an exception.** ``ConnectorAuthError``
  carries a code, not a body, and the body is where a failing token endpoint
  echoes the request — client secret included.
* **A refusal is recognised however it arrives.** A Slack-shaped API answers
  ``{"ok": false}`` with HTTP **200**, so trusting the status code alone would
  read a declined exchange as a success with no token in it.

The rest pins the config-driven behaviour that would otherwise be discovered at
consent time, in a user's browser, on a provider-branded error page.
"""
from __future__ import annotations

import logging
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.modules.agents import oauth
from app.modules.agents.catalog import ConnectorSpec
from app.shared.errors.app_error import ConfigurationError

_TOKEN_URL = "https://provider.test/token"


def _spec(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> ConnectorSpec:
    monkeypatch.setenv("TEST_CLIENT_ID", "client-id")
    monkeypatch.setenv("TEST_CLIENT_SECRET", "client-secret")
    fields: dict[str, object] = {
        "provider": "provider",
        "display_name": "Provider",
        "mcp_server_url": "https://mcp.provider.test/mcp",
        "authorize_endpoint": "https://provider.test/authorize",
        "token_endpoint": _TOKEN_URL,
        "token_endpoint_auth": "client_secret_post",
        "scopes": "read write",
        "client_id_env": "TEST_CLIENT_ID",
        "client_secret_env": "TEST_CLIENT_SECRET",
    }
    fields.update(overrides)
    return ConnectorSpec(**fields)  # type: ignore[arg-type]


def _transport(handler: object) -> httpx.MockTransport:
    return httpx.MockTransport(handler)  # type: ignore[arg-type]


async def _exchange(
    monkeypatch: pytest.MonkeyPatch, spec: ConnectorSpec, handler: object
) -> oauth.OAuthGrant:
    """Run ``exchange_code`` against a mock transport."""
    original = httpx.AsyncClient

    def _client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = _transport(handler)
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(oauth.httpx, "AsyncClient", _client)
    return await oauth.exchange_code(
        spec, code="the-code", redirect_uri="https://api.test/cb", nonce="nonce-1"
    )


# ── PKCE ──────────────────────────────────────────────────────────────────────


def test_the_verifier_matches_rfc_7636s_grammar() -> None:
    verifier = oauth.code_verifier("nonce-1")
    assert 43 <= len(verifier) <= 128
    assert set(verifier) <= set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    )


def test_the_verifier_is_derived_and_so_survives_a_round_trip() -> None:
    """Nothing stores it: the callback recomputes it from the state's nonce."""
    assert oauth.code_verifier("nonce-1") == oauth.code_verifier("nonce-1")
    assert oauth.code_verifier("nonce-1") != oauth.code_verifier("nonce-2")


def test_the_challenge_is_s256_of_the_verifier() -> None:
    import base64
    import hashlib

    verifier = oauth.code_verifier("nonce-1")
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).decode().rstrip("=")
    assert oauth.code_challenge(verifier) == expected


# ── authorize ─────────────────────────────────────────────────────────────────


def test_authorize_url_carries_pkce_and_the_state(monkeypatch: pytest.MonkeyPatch) -> None:
    url = oauth.authorize_url(
        _spec(monkeypatch), state="st.sig", redirect_uri="https://api.test/cb",
        nonce="nonce-1",
    )
    query = parse_qs(urlparse(url).query)

    assert query["response_type"] == ["code"]
    assert query["state"] == ["st.sig"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"] == [
        oauth.code_challenge(oauth.code_verifier("nonce-1"))
    ]
    assert query["scope"] == ["read write"]


def test_extra_authorize_params_are_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    """Atlassian's ``audience`` + ``prompt=consent`` is the reason this exists."""
    spec = _spec(
        monkeypatch,
        extra_authorize_params={"audience": "api.atlassian.com", "prompt": "consent"},
    )
    query = parse_qs(urlparse(
        oauth.authorize_url(spec, state="s", redirect_uri="https://api.test/cb", nonce="n")
    ).query)

    assert query["audience"] == ["api.atlassian.com"]
    assert query["prompt"] == ["consent"]


def test_extra_params_cannot_overwrite_the_security_bearing_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A catalog row is config, and config must not be able to disable PKCE or
    redirect the consent somewhere else."""
    spec = _spec(
        monkeypatch,
        extra_authorize_params={
            "redirect_uri": "https://evil.test/steal",
            "code_challenge_method": "plain",
            "state": "attacker-state",
        },
    )
    query = parse_qs(urlparse(
        oauth.authorize_url(spec, state="s", redirect_uri="https://api.test/cb", nonce="n")
    ).query)

    assert query["redirect_uri"] == ["https://api.test/cb"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["state"] == ["s"]


def test_an_unconfigured_provider_raises_before_the_browser_leaves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEST_CLIENT_ID", raising=False)
    spec = ConnectorSpec(
        provider="p", display_name="P", mcp_server_url="https://mcp.test/mcp",
        authorize_endpoint="https://p.test/a", token_endpoint=_TOKEN_URL,
        token_endpoint_auth="client_secret_post", client_id_env="TEST_CLIENT_ID",
    )
    with pytest.raises(ConfigurationError):
        oauth.authorize_url(spec, state="s", redirect_uri="https://api.test/cb", nonce="n")


# ── exchange ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_exchange_sends_the_code_verifier_and_reads_a_json_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, list[str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(parse_qs(request.content.decode()))
        return httpx.Response(200, json={
            "access_token": "at", "refresh_token": "rt",
            "expires_in": 3600, "scope": "read",
        })

    grant = await _exchange(monkeypatch, _spec(monkeypatch), handler)

    assert seen["code_verifier"] == [oauth.code_verifier("nonce-1")]
    assert seen["grant_type"] == ["authorization_code"]
    assert seen["client_secret"] == ["client-secret"]
    assert grant.access_token == "at"
    assert grant.refresh_token == "rt"
    assert grant.scope == "read"
    assert grant.expires_at is not None


@pytest.mark.asyncio
async def test_basic_auth_puts_the_secret_in_the_header_not_the_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Notion's token endpoint. The client_id stays in the body — providers that
    key on it there break without it."""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["form"] = parse_qs(request.content.decode())
        return httpx.Response(200, json={"access_token": "at"})

    spec = _spec(monkeypatch, token_endpoint_auth="client_secret_basic")
    await _exchange(monkeypatch, spec, handler)

    assert str(seen["auth"]).startswith("Basic ")
    assert "client_secret" not in seen["form"]  # type: ignore[operator]
    assert seen["form"]["client_id"] == ["client-id"]  # type: ignore[index]


@pytest.mark.asyncio
async def test_a_form_encoded_response_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """GitHub answers form-encoded unless asked for JSON — and "asked for JSON"
    is a catalog field a deployment can get wrong."""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="access_token=at&scope=repo&token_type=bearer",
            headers={"content-type": "application/x-www-form-urlencoded"},
        )

    grant = await _exchange(monkeypatch, _spec(monkeypatch), handler)
    assert grant.access_token == "at"
    assert grant.scope == "repo"


@pytest.mark.asyncio
async def test_a_slack_shaped_refusal_at_http_200_is_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error": "invalid_code"})

    with pytest.raises(oauth.ConnectorAuthError) as raised:
        await _exchange(monkeypatch, _spec(monkeypatch), handler)
    assert "invalid_code" in raised.value.message


@pytest.mark.asyncio
async def test_a_200_with_no_access_token_is_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"token_type": "bearer"})

    with pytest.raises(oauth.ConnectorAuthError):
        await _exchange(monkeypatch, _spec(monkeypatch), handler)


@pytest.mark.asyncio
async def test_the_failure_never_carries_the_response_body(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """A token endpoint that 400s often echoes the request back — and the request
    carried the client secret."""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={
            "error": "invalid_grant",
            "error_description": "sent client_secret=client-secret and code=the-code",
        })

    with caplog.at_level(logging.WARNING), pytest.raises(oauth.ConnectorAuthError) as raised:
        await _exchange(monkeypatch, _spec(monkeypatch), handler)

    logged = caplog.text
    assert "client-secret" not in raised.value.message
    assert "client-secret" not in logged
    assert "the-code" not in logged
    assert "invalid_grant" in logged  # the code, which is the actionable part


@pytest.mark.asyncio
async def test_an_unreachable_token_endpoint_is_a_502_not_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with pytest.raises(oauth.ConnectorAuthError) as raised:
        await _exchange(monkeypatch, _spec(monkeypatch), handler)
    assert raised.value.status == 502


# ── refresh block ─────────────────────────────────────────────────────────────


def test_refresh_block_carries_the_client_secret_and_granted_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This is what lets us not have a token store: Anthropic runs the refresh
    grant itself."""
    spec = _spec(monkeypatch)
    block = oauth.refresh_block(
        spec, oauth.OAuthGrant(access_token="at", refresh_token="rt", scope="read")
    )
    assert block == {
        "client_id": "client-id",
        "refresh_token": "rt",
        "token_endpoint": _TOKEN_URL,
        "token_endpoint_auth": {
            "type": "client_secret_post", "client_secret": "client-secret"
        },
        "scope": "read",
    }


def test_refresh_block_sends_the_granted_scope_not_the_requested_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refresh that widens scope is rejected by spec-compliant providers, and a
    user may well have unticked something on the consent screen."""
    spec = _spec(monkeypatch, scopes="read write admin")
    block = oauth.refresh_block(
        spec, oauth.OAuthGrant(access_token="at", refresh_token="rt", scope="read")
    )
    assert block is not None and block["scope"] == "read"


def test_a_public_client_sends_no_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = _spec(monkeypatch, token_endpoint_auth="none")
    block = oauth.refresh_block(
        spec, oauth.OAuthGrant(access_token="at", refresh_token="rt")
    )
    assert block is not None and block["token_endpoint_auth"] == {"type": "none"}


def test_no_refresh_token_means_no_refresh_block(monkeypatch: pytest.MonkeyPatch) -> None:
    """Correct only for providers whose access tokens do not expire. Everything
    else is a credential that will quietly stop working (§8)."""
    assert oauth.refresh_block(
        _spec(monkeypatch), oauth.OAuthGrant(access_token="at")
    ) is None


# ── account label ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"access_token": "at", "workspace_name": "Acme"}, "Acme"),
        ({"access_token": "at", "team": {"name": "Acme"}}, "Acme"),
        ({"access_token": "at"}, None),
        ({"access_token": "at", "workspace_name": "   "}, None),
    ],
)
async def test_the_account_label_is_best_effort(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, object], expected: str | None
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    grant = await _exchange(monkeypatch, _spec(monkeypatch), handler)
    assert grant.account_label == expected
