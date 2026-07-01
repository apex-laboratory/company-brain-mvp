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
from app.shared.errors.app_error import UnauthorizedError, ValidationError
from app.shared.helpers.crypto import hmac_sign


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
    ), patch.object(svc, "run_in_tenant", return_value=_AsyncCtx(None)), patch.object(
        svc, "enqueue", AsyncMock()
    ):
        await service.handle_callback(
            "zendesk", state=_valid_state(), code="c", installation_id="evil.com/x"
        )

    # the malicious query value is ignored; the stored subdomain reaches exchange_code
    assert fake.exchange_code.await_args.kwargs["installation_id"] == "acme"


def test_start_authorization_builds_signed_state_indirectly() -> None:
    # A round-trip of the signing scheme the service uses for state.
    nonce = "xyz"
    sig = hmac_sign(nonce, settings.jwt_access_secret)
    from app.shared.helpers.crypto import hmac_verify

    assert hmac_verify(nonce, sig, settings.jwt_access_secret) is True
    assert hmac_verify("different", sig, settings.jwt_access_secret) is False
