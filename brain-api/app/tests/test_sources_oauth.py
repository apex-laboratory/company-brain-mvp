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


class _ReturnToRepo:
    """Repo whose consumed state carries a stored (allowlisted) return_to."""

    def __init__(self, return_to: str | None) -> None:
        self._return_to = return_to

    async def consume_oauth_state(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return ResolvedState(
            id="os_1",
            user_id="usr_1",
            workspace_id="wrk_1",
            redirect_uri="https://cb/callback",
            return_to=self._return_to,
        )

    async def upsert_connection(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return "src_1"


async def _run_success_callback(return_to: str | None) -> str:
    service = SourcesService(repository=_ReturnToRepo(return_to))  # type: ignore[arg-type]
    fake = MagicMock()
    fake.exchange_code = AsyncMock(
        return_value=OAuthTokens(access_token="tok", external_account_id="ws_1")
    )
    session = MagicMock(commit=AsyncMock())
    with patch.object(svc, "get_integration", return_value=fake), patch.object(
        svc, "get_session", return_value=_AsyncCtx(session)
    ), patch.object(
        svc, "get_tenant_session", return_value=_AsyncCtx(session)
    ), patch.object(svc, "run_in_tenant", return_value=_AsyncCtx(None)), patch.object(
        svc, "enqueue", AsyncMock()
    ):
        return await service.handle_callback("notion", state=_valid_state(), code="c")


async def test_callback_success_redirects_to_stored_return_to() -> None:
    """The state-bound return_to picks the landing page (onboarding connect step)."""
    redirect = await _run_success_callback("/onboarding")
    assert redirect == f"{settings.frontend_url}/onboarding?connected=notion"


async def test_callback_success_without_return_to_keeps_default() -> None:
    """Omitted return_to preserves today's redirect exactly (backward compatible)."""
    redirect = await _run_success_callback(None)
    assert redirect == f"{settings.frontend_url}/settings/sources?connected=notion"


class _NeverConsumeRepo:
    """Repo that fails the test if the callback tries to consume the state."""

    def __init__(self, return_to: str | None = None) -> None:
        self._return_to = return_to

    async def consume_oauth_state(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("state must not be consumed on a consent-cancel callback")

    async def peek_oauth_state(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return self._return_to


async def test_callback_consent_cancel_redirects_without_burning_state() -> None:
    """A user declining the consent screen (error=access_denied, no code) must get a
    clean redirect back to the sources page — not a 500 — and the single-use state must
    stay unconsumed so nothing else breaks if the browser replays the URL."""
    service = SourcesService(repository=_NeverConsumeRepo())  # type: ignore[arg-type]
    session = MagicMock(commit=AsyncMock())
    with patch.object(svc, "get_session", return_value=_AsyncCtx(session)):
        redirect = await service.handle_callback(
            "notion", state=_valid_state(), code=None, error="access_denied"
        )
    assert redirect == f"{settings.frontend_url}/settings/sources?error=notion"


async def test_callback_consent_cancel_honors_stored_return_to() -> None:
    """A decline mid-onboarding must land back on onboarding: the stored return_to is
    read without consuming the state (peek, not consume)."""
    service = SourcesService(  # type: ignore[arg-type]
        repository=_NeverConsumeRepo(return_to="/onboarding")
    )
    session = MagicMock(commit=AsyncMock())
    with patch.object(svc, "get_session", return_value=_AsyncCtx(session)):
        redirect = await service.handle_callback(
            "notion", state=_valid_state(), code=None, error="access_denied"
        )
    assert redirect == f"{settings.frontend_url}/onboarding?error=notion"


class _NoDbRepo:
    """Repo that fails the test if the callback touches the DB at all."""

    async def consume_oauth_state(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("forged state must not reach the DB")

    async def peek_oauth_state(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("forged state must not reach the DB")


async def test_callback_consent_cancel_with_forged_state_uses_default() -> None:
    """On the decline leg a forged/unsigned state gets the default destination without
    any DB lookup — the peek is gated on the stateless signature check."""
    service = SourcesService(repository=_NoDbRepo())  # type: ignore[arg-type]
    redirect = await service.handle_callback(
        "notion", state="garbage-no-signature", code=None, error="access_denied"
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


class _CaptureStateRepo:
    """Repo that records the kwargs of the oauth_states INSERT."""

    def __init__(self) -> None:
        self.created: dict | None = None

    async def create_oauth_state(self, session, **kwargs):  # noqa: ANN001, ANN003
        self.created = kwargs


async def _run_start_authorization(monkeypatch, return_to: str | None) -> dict:
    """Drive start_authorization's happy path with a stub settings + fake repo."""
    from types import SimpleNamespace

    monkeypatch.setattr(
        svc,
        "settings",
        SimpleNamespace(
            notion_client_id="cid",
            notion_client_secret="sec",
            jwt_access_secret=settings.jwt_access_secret,
            oauth_redirect_base_url="https://api.example.com",
            oauth_state_ttl_seconds=600,
        ),
    )
    repo = _CaptureStateRepo()
    service = SourcesService(repository=repo)  # type: ignore[arg-type]
    fake = MagicMock()
    fake.authorize_url = MagicMock(return_value="https://provider/consent")
    auth = AuthContext(user_id="usr_1", workspace_id="wrk_1", role="admin")
    session = MagicMock(commit=AsyncMock())
    with patch.object(svc, "get_integration", return_value=fake), patch.object(
        svc, "get_session", return_value=_AsyncCtx(session)
    ):
        await service.start_authorization(auth, "notion", return_to=return_to)
    assert repo.created is not None
    return repo.created


async def test_start_authorization_persists_allowlisted_return_to(monkeypatch) -> None:
    created = await _run_start_authorization(monkeypatch, "/onboarding")
    assert created["return_to"] == "/onboarding"


@pytest.mark.parametrize(
    "evil",
    [
        "https://evil.com/onboarding",
        "//evil.com",
        "/onboarding/../admin",
        "/other",
        "javascript:alert(1)",
        "\\evil.com",
        "",
    ],
)
async def test_start_authorization_drops_non_allowlisted_return_to(
    monkeypatch, evil: str
) -> None:
    """Open-redirect guard: anything off the exact allowlist is stored as NULL, so the
    callback falls back to the default — never an external or unexpected destination."""
    created = await _run_start_authorization(monkeypatch, evil)
    assert created["return_to"] is None


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
