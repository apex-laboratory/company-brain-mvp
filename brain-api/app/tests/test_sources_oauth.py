"""Sources OAuth state security tests (KAN-2).

The signature check and the single-use DB consume are the two gates protecting the
callback. These verify both reject paths without a database (the repository is
faked / the signature path short-circuits before any DB call).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.modules.sources.service as svc
from app.config.settings import settings
from app.integrations.base import OAuthTokens
from app.modules.sources.repository import ResolvedState
from app.modules.sources.service import SourcesService
from app.shared.errors.app_error import (
    ConfigurationError,
    UnauthorizedError,
    ValidationError,
)
from app.shared.helpers.crypto import hmac_sign
from app.shared.middleware.authenticate import AuthContext


def _valid_state() -> str:
    nonce = "nonce-abc"
    return f"{nonce}.{hmac_sign(nonce, settings.jwt_access_secret)}"


class _AsyncCtx:
    def __init__(self, value: object) -> None:
        self._value = value

    async def __aenter__(self) -> object:
        return self._value

    async def __aexit__(self, *_: object) -> bool:
        return False


async def test_callback_rejects_unknown_provider() -> None:
    service = SourcesService()
    with pytest.raises(ValidationError):
        await service.handle_callback("dropbox", code="code", state=_valid_state())


async def test_callback_rejects_tampered_signature() -> None:
    service = SourcesService()
    # Valid format but the nonce no longer matches the signature.
    bad = f"tampered.{hmac_sign('nonce-abc', settings.jwt_access_secret)}"
    with pytest.raises(UnauthorizedError):
        await service.handle_callback("notion", code="code", state=bad)


async def test_callback_rejects_state_without_signature() -> None:
    service = SourcesService()
    with pytest.raises(UnauthorizedError):
        await service.handle_callback("notion", code="code", state="no-dot-here")


class _NoStateRepo:
    """Repository whose state lookup always misses (expired/used/unknown)."""

    async def consume_oauth_state(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None


async def test_callback_rejects_valid_signature_but_missing_state() -> None:
    service = SourcesService(repository=_NoStateRepo())  # type: ignore[arg-type]
    with pytest.raises(UnauthorizedError):
        await service.handle_callback("notion", code="code", state=_valid_state())


class _SubdomainRepo:
    """Repo whose consumed state carries a stored (validated) subdomain."""

    def __init__(self, subdomain: str) -> None:
        self._subdomain = subdomain

    async def consume_oauth_state(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return ResolvedState(
            id="os_1",
            user_id="usr_1",
            workspace_id="wrk_1",
            redirect_uri="https://cb/callback",
            subdomain=self._subdomain,
        )

    async def upsert_connection(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return "src_1"


async def test_callback_stored_subdomain_wins_over_query_installation_id() -> None:
    """Security: for subdomain-scoped providers the stored, server-validated subdomain
    must be used, never the attacker-influenceable query installation_id (which becomes
    a URL host and would otherwise redirect the token POST + client secret off-host)."""
    service = SourcesService(repository=_SubdomainRepo(subdomain="acme"))
    fake = MagicMock()
    fake.exchange_code = AsyncMock(
        return_value=OAuthTokens(access_token="tok", external_account_id="acme")
    )
    session = MagicMock(commit=AsyncMock())
    with patch.object(svc, "get_integration", return_value=fake), patch.object(
        svc, "get_session", return_value=_AsyncCtx(session)
    ), patch.object(
        svc, "get_tenant_session", return_value=_AsyncCtx(session)
    ), patch.object(svc, "run_in_tenant", return_value=_AsyncCtx(None)), patch.object(
        svc, "enqueue", AsyncMock()
    ):
        await service.handle_callback(
            "zendesk", state=_valid_state(), code="c", installation_id="evil.com/x"
        )

    # the malicious query value is ignored; the stored subdomain reaches exchange_code
    assert fake.exchange_code.await_args.kwargs["installation_id"] == "acme"


class _NeverConsumeRepo:
    """Repo that fails the test if the callback tries to consume the state."""

    async def consume_oauth_state(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("state must not be consumed on a consent-cancel callback")


async def test_callback_consent_cancel_redirects_without_burning_state() -> None:
    """A user declining the consent screen (error=access_denied, no code) must get a
    clean redirect back to the sources page — not a 500 — and the single-use state must
    stay unconsumed so nothing else breaks if the browser replays the URL."""
    service = SourcesService(repository=_NeverConsumeRepo())  # type: ignore[arg-type]
    redirect = await service.handle_callback(
        "notion", state=_valid_state(), code=None, error="access_denied"
    )
    assert redirect == f"{settings.frontend_url}/settings/sources?error=notion"


async def test_start_authorization_rejects_unconfigured_provider(monkeypatch) -> None:
    """With no client credentials configured the flow must 501 up front instead of
    bouncing the user to the provider with an empty client_id (opaque provider error
    page + an orphan oauth_states row per attempt)."""
    # The real settings singleton is frozen; swap the service's module reference for a
    # stub with empty Slack credentials.
    from types import SimpleNamespace

    monkeypatch.setattr(
        svc, "settings", SimpleNamespace(slack_client_id="", slack_client_secret="")
    )
    auth = AuthContext(user_id="usr_1", workspace_id="wrk_1", role="admin")
    with pytest.raises(ConfigurationError):
        await SourcesService().start_authorization(auth, "slack")


def test_every_integration_accepts_the_config_kwarg() -> None:
    """start_authorization always calls ``authorize_url(state, uri, config=...)``; a
    provider whose signature lacks the kwarg raises TypeError → 500 on every
    POST /sources/{provider}/authorize (launch blocker: four providers had it)."""
    from app.integrations import REGISTRY

    for provider, integration in REGISTRY.items():
        config = {"subdomain": "acme"} if provider == "zendesk" else None
        url = integration.authorize_url("state-1", "https://api.example.com/cb", config=config)
        assert isinstance(url, str) and url


def test_start_authorization_builds_signed_state_indirectly() -> None:
    # A round-trip of the signing scheme the service uses for state.
    nonce = "xyz"
    sig = hmac_sign(nonce, settings.jwt_access_secret)
    from app.shared.helpers.crypto import hmac_verify

    assert hmac_verify(nonce, sig, settings.jwt_access_secret) is True
    assert hmac_verify("different", sig, settings.jwt_access_secret) is False
