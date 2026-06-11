from __future__ import annotations

import time

import pytest
from fastapi.security import HTTPAuthorizationCredentials
from jose import jwt
from starlette.requests import Request

from app.config.settings import settings
from app.shared.errors.app_error import UnauthorizedError
from app.shared.middleware import authenticate
from app.shared.middleware.authenticate import AuthContext, get_auth_context


def _request(headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": headers or [],
            "state": {},
        }
    )


def _bearer(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def _encode(claims: dict[str, object]) -> str:
    return jwt.encode(claims, settings.jwt_access_secret, algorithm="HS256")


@pytest.mark.asyncio
async def test_resolves_valid_jwt() -> None:
    token = _encode(
        {
            "sub": "usr_1",
            "workspace_id": "wrk_1",
            "role": "admin",
            "scopes": ["brain:query"],
            "exp": int(time.time()) + 900,
        }
    )
    auth = await get_auth_context(_request(), _bearer(token))
    assert auth.kind == "jwt"
    assert auth.user_id == "usr_1"
    assert auth.workspace_id == "wrk_1"
    assert auth.role == "admin"
    assert auth.scopes == ["brain:query"]


@pytest.mark.asyncio
async def test_rejects_jwt_without_exp_claim() -> None:
    # Correctly signed and otherwise complete, but no exp — must not become a
    # permanent credential.
    token = _encode({"sub": "usr_1", "workspace_id": "wrk_1", "role": "admin"})
    with pytest.raises(UnauthorizedError):
        await get_auth_context(_request(), _bearer(token))


@pytest.mark.asyncio
async def test_rejects_expired_jwt() -> None:
    token = _encode(
        {
            "sub": "usr_1",
            "workspace_id": "wrk_1",
            "role": "admin",
            "exp": int(time.time()) - 1,
        }
    )
    with pytest.raises(UnauthorizedError):
        await get_auth_context(_request(), _bearer(token))


@pytest.mark.asyncio
async def test_rejects_token_signed_with_wrong_secret() -> None:
    token = jwt.encode({"sub": "usr_1"}, "a-different-secret-key-32-characters!", algorithm="HS256")
    with pytest.raises(UnauthorizedError):
        await get_auth_context(_request(), _bearer(token))


@pytest.mark.asyncio
async def test_rejects_jwt_missing_required_claims() -> None:
    token = jwt.encode({"sub": "usr_1"}, settings.jwt_access_secret, algorithm="HS256")
    with pytest.raises(UnauthorizedError):
        await get_auth_context(_request(), _bearer(token))


@pytest.mark.asyncio
async def test_resolves_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_from_api_key(raw_key: str) -> AuthContext:
        assert raw_key == "hph_live_abc"
        return AuthContext(
            user_id="usr_k",
            workspace_id="wrk_k",
            role="viewer",
            scopes=["brain:query"],
            kind="api_key",
        )

    async def no_rate_limit(_request: Request) -> None:
        return None

    monkeypatch.setattr(authenticate, "_from_api_key", fake_from_api_key)
    monkeypatch.setattr(authenticate, "enforce_api_key_rate_limit", no_rate_limit)
    request = _request([(b"x-api-key", b"hph_live_abc")])
    auth = await get_auth_context(request, None)
    assert auth.kind == "api_key"
    assert auth.workspace_id == "wrk_k"
    assert auth.scopes == ["brain:query"]


@pytest.mark.asyncio
async def test_no_credentials_raises_unauthorized() -> None:
    with pytest.raises(UnauthorizedError):
        await get_auth_context(_request(), None)
