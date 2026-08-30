"""Resolve the caller into an ``AuthContext`` (BACKEND_BEST_PRACTICES.md §7).

Two credential types:
  1. Dashboard JWT — ``Authorization: Bearer <accessToken>`` (HS256, stateless).
  2. Workspace API key — ``X-API-Key: hph_live_...`` (looked up by SHA-256 hash).

A request with neither (or invalid credentials) raises ``UnauthorizedError``.
Authorization (role/scope) is enforced separately in ``authorize.py`` and
backstopped by RLS in the DB.
"""
from __future__ import annotations

import time
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
from app.shared.middleware.rate_limit import enforce_api_key_rate_limit

bearer = HTTPBearer(auto_error=False)

# API keys authenticate against the workspace, not a member seat; they act with
# least-privilege role for RLS purposes and are gated by scopes in authorize.py.
_API_KEY_ROLE = "viewer"

_repository = AuthRepository()

# Resolved-key cache: sha256(key) → (expiry, AuthContext). Every API-key request
# used to cost a privileged-pool SELECT + UPDATE + COMMIT before the handler ran;
# a 30s TTL removes that round trip from the hot path. Positive entries only —
# invalid keys are never cached (and stay covered by the per-IP probe limit).
# Trade-off: a revoked/expired key stays usable for up to TTL per process, and
# ``last_used_at`` advances at most once per TTL window (it's a coarse "is this
# key alive" signal, not an audit log).
_API_KEY_CACHE_TTL_SECONDS = 30.0
_api_key_cache: dict[bytes, tuple[float, AuthContext]] = {}


@dataclass(frozen=True)
class AuthContext:
    # ``workspace_id``/``role`` are ``None`` for a freshly signed-up user who has
    # no workspace yet (their JWT carries null claims). API-key contexts always
    # carry both. Consumers must treat the no-workspace case explicitly:
    # ``require_role`` denies it (rank 0) and tenant-scoped queries must not run.
    user_id: str
    workspace_id: str | None
    role: str | None
    scopes: list[str] = field(default_factory=list)
    kind: Literal["jwt", "api_key"] = "jwt"
    # Non-dashboard credential (agent, plugin, CI). When true, ``query_brain``
    # skips query-driven extraction and stores no query text. Dashboard JWTs
    # leave this False; API keys carry the per-credential ``api_keys`` column,
    # which itself defaults true.
    agent_origin: bool = False


def _from_jwt(token: str) -> AuthContext | None:
    try:
        payload = jwt.decode(
            token,
            settings.jwt_access_secret,
            algorithms=["HS256"],
            # python-jose only *validates* exp when present; require_exp rejects a
            # token that omits it, so a well-signed but unexpiring token can't
            # become a permanent credential. (require_exp is jose's boolean flag;
            # the PyJWT-style {"require": [...]} list is silently ignored here, and
            # jose can't require custom claims — those are checked below.)
            options={"require_exp": True},
        )
    except JWTError:
        # Bad signature, expired, or no exp claim.
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
        # Well-signed and unexpired but missing an app claim — treat as invalid.
        return None


async def _from_api_key(raw_key: str) -> AuthContext | None:
    key_hash = sha256_hash(raw_key)
    entry = _api_key_cache.get(key_hash)
    if entry is not None and time.monotonic() < entry[0]:
        return entry[1]
    async with get_session() as session:
        resolved = await _repository.resolve_api_key_by_hash(session, key_hash)
    if resolved is None:
        _api_key_cache.pop(key_hash, None)  # drop a stale positive on revocation
        return None
    auth = AuthContext(
        user_id=resolved.created_by,
        workspace_id=resolved.workspace_id,
        role=_API_KEY_ROLE,
        scopes=resolved.scopes,
        kind="api_key",
        agent_origin=resolved.agent_origin,
    )
    _api_key_cache[key_hash] = (time.monotonic() + _API_KEY_CACHE_TTL_SECONDS, auth)
    return auth


async def authenticate_api_key(raw_key: str) -> AuthContext | None:
    """Resolve a raw ``X-API-Key`` into an ``AuthContext`` (or ``None`` if invalid).

    The public entry point for credential resolution outside the HTTP dependency
    graph — the MCP ``query_brain`` tool uses it to authenticate agents over the
    SSE transport with the same key hashing and workspace binding as the REST API.
    """
    return await _from_api_key(raw_key)


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
        await enforce_api_key_rate_limit(request)
        auth = await _from_api_key(api_key)
        if auth is not None:
            request.state.auth = auth
            return auth

    raise UnauthorizedError()


