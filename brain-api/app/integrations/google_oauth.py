"""Google OAuth 2.0 integration.

Implements the code-exchange + profile-fetch leg of the server-side OAuth flow.
All HTTP calls use a 10-second timeout; callers should catch ``httpx.HTTPError``
for network failures and ``httpx.HTTPStatusError`` for provider-side errors.
"""
from __future__ import annotations

from urllib.parse import urlencode

import httpx

from app.integrations import OAuthError, OAuthProfile

_TOKEN_URL = "https://oauth2.googleapis.com/token"
_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"


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
        payload = resp.json()
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not token:
            error = payload.get("error") if isinstance(payload, dict) else None
            raise OAuthError(f"Google token exchange failed: {error or 'no access_token'}")
        return str(token)


def build_authorize_url(*, client_id: str, redirect_uri: str, state: str) -> str:
    """Build Google's authorization URL with the required query params."""
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
    }
    return f"{_AUTHORIZE_URL}?{urlencode(params)}"


async def fetch_profile(access_token: str) -> OAuthProfile:
    """Fetch the authenticated user's email and name from Google."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
        resp = await client.get(
            _USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        data = resp.json()
    email = data.get("email") if isinstance(data, dict) else None
    if not email:
        raise OAuthError("Google userinfo response did not include an email.")
    return OAuthProfile(email=str(email), name=data.get("name"))
