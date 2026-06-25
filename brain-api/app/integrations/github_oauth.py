"""GitHub OAuth 2.0 integration.

Implements code-exchange + profile-fetch. GitHub separates the email list from
the user profile, so the user and email lookups are fetched concurrently to
resolve the primary verified email.
"""
from __future__ import annotations

import asyncio
from urllib.parse import urlencode

import httpx

from app.integrations import OAuthError, OAuthProfile

_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
_TOKEN_URL = "https://github.com/login/oauth/access_token"
_USER_URL = "https://api.github.com/user"
_EMAILS_URL = "https://api.github.com/user/emails"


async def exchange_code(
    *,
    code: str,
    redirect_uri: str,
    client_id: str,
    client_secret: str,
) -> str:
    """Exchange an authorization code for a GitHub access token."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
        resp = await client.post(
            _TOKEN_URL,
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
            },
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        # GitHub signals a bad/expired code with HTTP 200 + an ``error`` body
        # (no ``access_token``), which raise_for_status does not catch.
        payload = resp.json()
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not token:
            error = payload.get("error") if isinstance(payload, dict) else None
            raise OAuthError(f"GitHub token exchange failed: {error or 'no access_token'}")
        return str(token)


def build_authorize_url(*, client_id: str, redirect_uri: str, state: str) -> str:
    """Build GitHub's authorization URL with the required query params."""
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": "user:email",
        "state": state,
    }
    return f"{_AUTHORIZE_URL}?{urlencode(params)}"


async def fetch_profile(access_token: str) -> OAuthProfile:
    """Fetch the authenticated user's primary email and display name from GitHub."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
        # The user profile and the email list are independent; fetch concurrently.
        user_resp, emails_resp = await asyncio.gather(
            client.get(_USER_URL, headers=headers),
            client.get(_EMAILS_URL, headers=headers),
        )
        user_resp.raise_for_status()
        emails_resp.raise_for_status()

    name: str | None = user_resp.json().get("name")
    email = _primary_email(emails_resp.json())
    return OAuthProfile(email=email, name=name)


def _primary_email(entries: object) -> str:
    """Return the primary verified email, else any verified email.

    Only verified emails are trusted: the callback resolves the user by email, so
    returning an unverified address would let an attacker who added (but never
    confirmed) a victim's email to their GitHub account take over that account.
    Raises ``OAuthError`` when no verified email is available.
    """
    if not isinstance(entries, list) or not entries:
        raise OAuthError("GitHub returned no email addresses for the account.")
    for e in entries:
        if e.get("primary") and e.get("verified"):
            return str(e["email"])
    for e in entries:
        if e.get("verified"):
            return str(e["email"])
    raise OAuthError("GitHub returned no verified email for this account.")
