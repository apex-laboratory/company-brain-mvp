"""Signed OAuth ``state`` tokens (BACKEND_BEST_PRACTICES.md §7).

Both the login SSO flow (``modules/auth``) and the source-connection flow
(``modules/sources``) sign a short-lived JWT ``state`` (HS256, ``JWT_ACCESS_SECRET``)
carrying the originating user/workspace/provider/redirect_uri so the callback can
verify the round-trip. This module is the single place that token is built and
decoded, so any hardening (claims, algorithm, expiry policy) changes in one place
for every flow.

The DB single-use record (``oauth_states``) is owned by the auth repository and
shared by both flows; this helper only handles the signed token itself. Callers
remain responsible for the per-flow checks (provider, redirect_uri, ownership,
mode) against the stored row after decoding.
"""
from __future__ import annotations

from datetime import datetime
from secrets import token_urlsafe
from typing import Any

from jose import JWTError, jwt

from app.config.settings import settings
from app.shared.errors.app_error import UnauthorizedError

_ALGORITHM = "HS256"


def encode_state(
    *,
    user_id: str | None,
    workspace_id: str | None,
    provider: str,
    redirect_uri: str,
    mode: str,
    expires_at: datetime,
    **extra_claims: Any,
) -> str:
    """Sign an OAuth state token expiring at ``expires_at``.

    ``extra_claims`` carries flow-specific data (e.g. the requested ``scopes`` for
    a source connection) so the callback can recover it without a second store.
    """
    payload: dict[str, Any] = {
        "user_id": user_id,
        "workspace_id": workspace_id,
        "provider": provider,
        "redirect_uri": redirect_uri,
        "mode": mode,
        "exp": int(expires_at.timestamp()),
        # JWT signing is deterministic: without a per-request identifier, two
        # otherwise identical starts in one second serialize to the same token
        # (``exp`` is second-precision) and collide with oauth_states.state_hash.
        "jti": token_urlsafe(32),
        **extra_claims,
    }
    token: str = jwt.encode(payload, settings.jwt_access_secret, algorithm=_ALGORITHM)
    return token


def decode_state(raw: str) -> dict[str, Any]:
    """Verify signature + expiry and return the claims.

    Raises ``UnauthorizedError`` on a bad signature, a missing/expired ``exp``, or
    any malformed token — a generic 401 so callers never reveal which check failed.
    """
    try:
        claims: dict[str, Any] = jwt.decode(
            raw,
            settings.jwt_access_secret,
            algorithms=[_ALGORITHM],
            options={"require_exp": True},
        )
    except JWTError as exc:
        raise UnauthorizedError("Invalid OAuth state") from exc
    return claims
