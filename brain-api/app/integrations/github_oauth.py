"""GitHub OAuth 2.0 integration.

Implements code-exchange + profile-fetch. GitHub separates the email list from
the user profile, so two sequential requests are made to resolve the primary
verified email.
"""
from __future__ import annotations

import httpx

from app.integrations import OAuthError, OAuthProfile

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
        # GitHub signals token errors (bad/expired code) with HTTP 200 and a body
        # like ``{"error": "bad_verification_code"}`` — raise_for_status passes, so
        # a missing access_token must be handled explicitly rather than KeyError'ing
        # into an unhandled 500.
        payload = resp.json()
        token = payload.get("access_token")
        if not token:
            raise OAuthError(
                f"GitHub token exchange failed: {payload.get('error', 'unknown')}"
            )
        return str(token)


async def fetch_profile(access_token: str) -> OAuthProfile:
    """Fetch the authenticated user's primary email and display name from GitHub."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
        user_resp = await client.get(_USER_URL, headers=headers)
        user_resp.raise_for_status()
        emails_resp = await client.get(_EMAILS_URL, headers=headers)
        emails_resp.raise_for_status()

    name: str | None = user_resp.json().get("name")
    email = _primary_email(emails_resp.json())
    return OAuthProfile(email=email, name=name)


def _primary_email(entries: list[dict[str, object]]) -> str:
    """Return the primary verified email, else any verified email.

    Only verified emails are trusted: the callback resolves the user by email, so
    returning an unverified address would let an attacker who added (but never
    confirmed) a victim's email to their GitHub account take over that account.
    Raises ``OAuthError`` when no verified email is available.
    """
    for e in entries:
        if e.get("primary") and e.get("verified"):
            return str(e["email"])
    for e in entries:
        if e.get("verified"):
            return str(e["email"])
    raise OAuthError("GitHub returned no verified email for this account.")
