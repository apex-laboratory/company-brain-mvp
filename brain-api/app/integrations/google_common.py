"""Shared Google OAuth 2.0 helpers for the Drive + Gmail connectors (KAN-2).

Both connectors use **one** Google Cloud OAuth client (web-server flow). The Drive
and Gmail integrations are thin wrappers that call these helpers with their own
scope, so the OAuth dance lives in one place.

* ``authorize_url`` sets ``access_type=offline`` + ``prompt=consent`` so Google
  returns a refresh token, and ``include_granted_scopes`` so connecting the second
  provider augments the same grant rather than replacing it.
* Access tokens expire in ~1h; ``refresh`` re-mints from the (normally stable)
  refresh token. Google rarely rotates the refresh token, but when it does we return
  the new one so ``source_sync`` can persist it.
* ``external_account_id`` is the Google account **email** (from userinfo). Gmail
  push deliveries carry ``emailAddress``, so webhook routing matches against it.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx

from app.config.settings import settings
from app.integrations.base import OAuthTokens, http_client

_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
_OIDC_SCOPES = "openid email"


def authorize_url(scope: str, state: str, redirect_uri: str) -> str:
    """Build the Google consent URL for ``scope`` (offline + forced consent)."""
    query = urlencode(
        {
            "client_id": settings.google_client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": f"{scope} {_OIDC_SCOPES}",
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
        }
    )
    return f"{_AUTH_URL}?{query}"


async def _account_email(access_token: str) -> str | None:
    """Resolve the Google account email (→ ``external_account_id``)."""
    resp = await http_client().get(
        _USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}
    )
    resp.raise_for_status()
    return resp.json().get("email")


def _tokens_from(
    data: dict, *, account_email: str | None, refresh_token: str | None
) -> OAuthTokens:
    expires_at: datetime | None = None
    if data.get("expires_in"):
        expires_at = datetime.now(UTC) + timedelta(seconds=int(data["expires_in"]))
    return OAuthTokens(
        access_token=data["access_token"],
        refresh_token=refresh_token,
        expires_at=expires_at,
        external_account_id=account_email,
        scopes=(data.get("scope") or "").split(),
        raw={**data, "workspace_name": account_email or "Google"},
    )


async def exchange_code(code: str, redirect_uri: str) -> OAuthTokens:
    """Exchange an authorization ``code`` for tokens; resolve the account email."""
    resp = await http_client().post(
        _TOKEN_URL,
        data={
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
    )
    resp.raise_for_status()
    data = resp.json()
    email = await _account_email(data["access_token"])
    return _tokens_from(data, account_email=email, refresh_token=data.get("refresh_token"))


async def refresh(refresh_token: str) -> OAuthTokens:
    """Re-mint an access token. Returns a rotated refresh token only if Google sends
    one (it usually doesn't); ``source_sync`` preserves the stored value otherwise."""
    resp = await http_client().post(
        _TOKEN_URL,
        data={
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
    )
    resp.raise_for_status()
    data = resp.json()
    return _tokens_from(data, account_email=None, refresh_token=data.get("refresh_token"))


def expiry_from_ms(value: str | int | None) -> datetime | None:
    """Convert a Google watch ``expiration`` (ms epoch) to an aware UTC datetime."""
    if not value:
        return None
    return datetime.fromtimestamp(int(value) / 1000, UTC)


async def revoke(token: str) -> None:
    """Best-effort token revocation; disconnect is local-only on failure."""
    try:
        await http_client().post(_REVOKE_URL, data={"token": token})
    except httpx.HTTPError:
        return None
    return None
