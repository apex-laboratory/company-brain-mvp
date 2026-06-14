"""Pure token primitives (BACKEND_BEST_PRACTICES.md §7).

No DB or framework dependencies — just minting an access JWT and generating an
opaque refresh secret. The service layer persists the refresh token's *hash*;
the raw value here only ever leaves the process in the response body / cookie.
"""
from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

from jose import jwt

from app.config.settings import settings

# 48 bytes of entropy -> 64-char urlsafe string. Never stored; only its sha256 is.
_REFRESH_TOKEN_BYTES = 48


def mint_access_token(
    *,
    user_id: str,
    workspace_id: str | None,
    role: str | None,
    scopes: list[str] | None = None,
) -> str:
    """Sign a short-lived HS256 access token.

    ``workspace_id``/``role`` are ``None`` before onboarding (the signup token),
    and populated once the caller has an active workspace membership. ``exp`` is
    always present so the token cannot become a permanent credential
    (authenticate.py rejects tokens without it).
    """
    now = datetime.now(UTC)
    payload: dict[str, object] = {
        "sub": user_id,
        "workspace_id": workspace_id,
        "role": role,
        "scopes": scopes or [],
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=settings.access_token_ttl_seconds)).timestamp()),
    }
    token: str = jwt.encode(payload, settings.jwt_access_secret, algorithm="HS256")
    return token


def generate_refresh_token() -> str:
    """Return a cryptographically random, urlsafe refresh token (raw value)."""
    return secrets.token_urlsafe(_REFRESH_TOKEN_BYTES)
