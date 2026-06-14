"""Third-party OAuth provider integrations.

Each sub-module owns the HTTP exchange (code → access_token → profile) for one
provider. All calls use ``httpx.AsyncClient`` with a 10-second timeout so a
hung provider never stalls the request indefinitely.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OAuthProfile:
    """Normalised user identity returned by every provider integration."""

    email: str
    name: str | None
