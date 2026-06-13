"""Sources OAuth state security tests (KAN-2).

The signature check and the single-use DB consume are the two gates protecting the
callback. These verify both reject paths without a database (the repository is
faked / the signature path short-circuits before any DB call).
"""
from __future__ import annotations

import pytest

from app.config.settings import settings
from app.modules.sources.service import SourcesService
from app.shared.errors.app_error import UnauthorizedError, ValidationError
from app.shared.helpers.crypto import hmac_sign


def _valid_state() -> str:
    nonce = "nonce-abc"
    return f"{nonce}.{hmac_sign(nonce, settings.jwt_access_secret)}"


async def test_callback_rejects_unknown_provider() -> None:
    service = SourcesService()
    with pytest.raises(ValidationError):
        await service.handle_callback("dropbox", "code", _valid_state())


async def test_callback_rejects_tampered_signature() -> None:
    service = SourcesService()
    # Valid format but the nonce no longer matches the signature.
    bad = f"tampered.{hmac_sign('nonce-abc', settings.jwt_access_secret)}"
    with pytest.raises(UnauthorizedError):
        await service.handle_callback("notion", "code", bad)


async def test_callback_rejects_state_without_signature() -> None:
    service = SourcesService()
    with pytest.raises(UnauthorizedError):
        await service.handle_callback("notion", "code", "no-dot-here")


class _NoStateRepo:
    """Repository whose state lookup always misses (expired/used/unknown)."""

    async def consume_oauth_state(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None


async def test_callback_rejects_valid_signature_but_missing_state() -> None:
    service = SourcesService(repository=_NoStateRepo())  # type: ignore[arg-type]
    with pytest.raises(UnauthorizedError):
        await service.handle_callback("notion", "code", _valid_state())


def test_start_authorization_builds_signed_state_indirectly() -> None:
    # A round-trip of the signing scheme the service uses for state.
    nonce = "xyz"
    sig = hmac_sign(nonce, settings.jwt_access_secret)
    from app.shared.helpers.crypto import hmac_verify

    assert hmac_verify(nonce, sig, settings.jwt_access_secret) is True
    assert hmac_verify("different", sig, settings.jwt_access_secret) is False
