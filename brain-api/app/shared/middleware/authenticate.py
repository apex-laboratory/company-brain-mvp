"""Resolve the caller into an ``AuthContext`` (BACKEND_BEST_PRACTICES.md §7).

Two credential types:
  1. Dashboard JWT — ``Authorization: Bearer <accessToken>`` (HS256, stateless).
  2. Workspace API key — ``X-API-Key: hph_live_...`` (looked up by SHA-256 hash).

A request with neither (or invalid credentials) raises ``UnauthorizedError``.
Authorization (role/scope) is enforced separately in ``authorize.py`` and
backstopped by RLS in the DB.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from app.config.database import get_session
from app.config.settings import settings
from app.modules.auth.repository import AuthRepository
from app.shared.errors.app_error import UnauthorizedError
from app.shared.helpers.crypto import sha256_hash

bearer = HTTPBearer(auto_error=False)

# API keys authenticate against the workspace, not a member seat; they act with
# least-privilege role for RLS purposes and are gated by scopes in authorize.py.
_API_KEY_ROLE = "viewer"

_repository = AuthRepository()


@dataclass(frozen=True)
class AuthContext:
    user_id: str
    workspace_id: str
    role: str
    scopes: list[str] = field(default_factory=list)
    kind: Literal["jwt", "api_key"] = "jwt"


def _from_jwt(token: str) -> AuthContext | None:
    try:
        payload = jwt.decode(token, settings.jwt_access_secret, algorithms=["HS256"])
    except JWTError:
        return None
    try:
        return AuthContext(
            user_id=payload["sub"],
            workspace_id=payload["workspace_id"],
            role=payload["role"],
            scopes=list(payload.get("scopes", [])),
            kind="jwt",
        )
    except KeyError:
        # Token is well-signed but missing required claims — treat as invalid.
        return None


async def _from_api_key(raw_key: str) -> AuthContext | None:
    async with get_session() as session:
        resolved = await _repository.resolve_api_key_by_hash(session, sha256_hash(raw_key))
    if resolved is None:
        return None
    return AuthContext(
        user_id=resolved.created_by,
        workspace_id=resolved.workspace_id,
        role=_API_KEY_ROLE,
        scopes=resolved.scopes,
        kind="api_key",
    )


async def get_auth_context(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> AuthContext:
    """FastAPI dependency: resolve JWT bearer, then X-API-Key, else 401."""
    if credentials is not None and credentials.scheme.lower() == "bearer":
        auth = _from_jwt(credentials.credentials)
        if auth is not None:
            request.state.auth = auth
            return auth

    api_key = request.headers.get("X-API-Key")
    if api_key:
        auth = await _from_api_key(api_key)
        if auth is not None:
            request.state.auth = auth
            return auth

    raise UnauthorizedError()
