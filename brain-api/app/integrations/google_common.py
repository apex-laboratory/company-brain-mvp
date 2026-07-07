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

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx

from app.config.settings import settings
from app.integrations.base import OAuthTokens, http_client

log = logging.getLogger(__name__)

# The Google connectors that deliver via push (Drive changes.watch / Gmail users.watch)
# and share this OAuth client. Single source of truth so a new Google connector is wired
# for watch-registration + push routing in one place instead of drifting across call sites.
GOOGLE_PUSH_PROVIDERS = ("google_drive", "gmail")

_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
_OIDC_SCOPES = "openid email"


class QuotaExceededError(Exception):
    """Google reported a rate-limit/quota error (429, or 403 with a quota reason)
    and retries were exhausted.

    Deliberately **not** an ``httpx.HTTPStatusError``: ``source_sync`` classifies
    401/403 status errors as broken auth (re-auth required), but a quota 403 is
    transient — raising this instead lands in the generic-exception path, which
    holds the cursor and lets ARQ retry the sweep later.
    """


_MAX_ATTEMPTS = 4  # one try + three retries on 429/quota-403/5xx
_MAX_RETRY_AFTER = 30.0  # cap a single honored Retry-After sleep (seconds)
# 403 reasons that mean "quota", not "permission denied":
# https://developers.google.com/drive/api/guides/handle-errors
_QUOTA_REASONS = frozenset(
    {"userRateLimitExceeded", "rateLimitExceeded", "dailyLimitExceeded"}
)


def _is_quota_error(resp: httpx.Response) -> bool:
    if resp.status_code == 429:
        return True
    if resp.status_code != 403:
        return False
    try:
        error = resp.json().get("error") or {}
    except ValueError:
        return False
    if error.get("status") == "RESOURCE_EXHAUSTED":
        return True
    reasons = {
        e.get("reason") for e in error.get("errors", []) if isinstance(e, dict)
    }
    return bool(reasons & _QUOTA_REASONS)


def _retry_delay(resp: httpx.Response, attempt: int) -> float:
    """Honor Retry-After when present (clamped); otherwise exponential backoff."""
    header = resp.headers.get("Retry-After")
    if header:
        try:
            return max(1.0, min(float(header), _MAX_RETRY_AFTER))
        except ValueError:
            pass
    return float(2**attempt)


async def api_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict | None = None,
    json: dict | None = None,
) -> httpx.Response:
    """Issue a Google API request with quota-aware retry.

    429s, quota 403s, and 5xxs are retried with backoff (Retry-After honored).
    Exhausted quota retries raise :class:`QuotaExceededError` so callers upstream
    don't mistake a rate limit for revoked auth; every other error status raises
    ``httpx.HTTPStatusError`` as before.
    """
    for attempt in range(_MAX_ATTEMPTS):
        resp = await http_client().request(
            method, url, headers=headers, params=params, json=json
        )
        quota = _is_quota_error(resp)
        if not quota and resp.status_code < 500:
            resp.raise_for_status()
            return resp
        if attempt + 1 < _MAX_ATTEMPTS:
            delay = _retry_delay(resp, attempt)
            log.warning(
                "google api %s %s → %d; retrying in %.1fs (attempt %d/%d)",
                method, url, resp.status_code, delay, attempt + 1, _MAX_ATTEMPTS,
            )
            await asyncio.sleep(delay)
            continue
        if quota:
            raise QuotaExceededError(
                f"{method} {url} rate-limited after {_MAX_ATTEMPTS} attempts"
            )
        resp.raise_for_status()
    raise AssertionError("unreachable")  # pragma: no cover


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
