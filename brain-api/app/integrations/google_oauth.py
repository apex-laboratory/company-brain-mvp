"""Google OAuth 2.0 integration.

Implements the code-exchange + profile-fetch leg of the server-side OAuth flow.
All HTTP calls use a 10-second timeout; callers should catch ``httpx.HTTPError``
for network failures and ``httpx.HTTPStatusError`` for provider-side errors.
"""
from __future__ import annotations

import httpx

from app.integrations import OAuthProfile

_TOKEN_URL = "https://oauth2.googleapis.com/token"
_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"


async def exchange_code(
    *,
    code: str,
    redirect_uri: str,
    client_id: str,
    client_secret: str,
) -> str:
    """Exchange an authorization code for a Google access token."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
        resp = await client.post(
            _TOKEN_URL,
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        resp.raise_for_status()
        return str(resp.json()["access_token"])


async def fetch_profile(access_token: str) -> OAuthProfile:
    """Fetch the authenticated user's email and name from Google."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
        resp = await client.get(
            _USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        data = resp.json()
    return OAuthProfile(email=data["email"], name=data.get("name"))
