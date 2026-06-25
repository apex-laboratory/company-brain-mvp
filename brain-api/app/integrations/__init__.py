"""Third-party OAuth provider integrations.

Each sub-module owns the HTTP exchange (code → access_token → profile) for one
provider. All calls use ``httpx.AsyncClient`` with a 10-second timeout so a
hung provider never stalls the request indefinitely.
"""
from __future__ import annotations

from dataclasses import dataclass


class OAuthError(Exception):
    """A provider returned a malformed or error response.

    Raised when an exchange/profile call gets an HTTP 200 the provider uses to
    signal failure — e.g. GitHub answers ``{"error": "bad_verification_code"}``
    with status 200 and no ``access_token`` — or when a required field (token,
    email) is missing. ``raise_for_status`` does not catch these, so callers
    that only handle ``httpx.HTTPError`` would otherwise see a raw
    ``KeyError``/``IndexError`` surface as a 500.
    """


@dataclass(frozen=True)
class OAuthProfile:
    """Normalised user identity returned by every provider integration."""

    email: str
    name: str | None
