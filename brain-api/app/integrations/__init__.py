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


class OAuthError(Exception):
    """Provider returned a response we can't trust (error body, unverified or
    missing email, …).

    Distinct from ``httpx.HTTPError`` (a transport/status failure): the HTTP call
    succeeded but the *payload* is unusable. Carries the status/code/message the
    callback should surface so a provider-data problem never leaks as a 500.
    """

    def __init__(
        self, message: str, *, status: int = 502, code: str = "provider_error"
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
