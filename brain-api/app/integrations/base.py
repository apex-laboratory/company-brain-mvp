"""Source integration protocol + canonical data shapes (BACKEND_BEST_PRACTICES.md §2, §12).

A ``SourceIntegration`` wraps one provider's OAuth dance and read-only fetch. The
sources module (OAuth lifecycle), the webhooks module (signature verification),
and the ARQ sync jobs (incremental fetch) all program against this protocol — never
against a concrete provider SDK.

All outbound HTTP shares a single timeout-bounded ``httpx.AsyncClient`` (per §12:
every outbound call sets a timeout). Concrete integrations get the client via
``http_client()`` so tests can swap in a ``MockTransport``.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

import httpx

# Default outbound timeout for all provider calls. Connectors that need a longer
# read (e.g. large block fetches) pass their own per-request timeout.
DEFAULT_TIMEOUT = httpx.Timeout(10.0, connect=5.0)

_client: httpx.AsyncClient | None = None


def http_client() -> httpx.AsyncClient:
    """Return the process-wide async HTTP client (lazy, timeout-bounded)."""
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)
    return _client


async def close_http_client() -> None:
    """Close the shared client on shutdown."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


@dataclass(frozen=True)
class OAuthTokens:
    """Tokens returned by a provider's token endpoint."""

    access_token: str
    refresh_token: str | None = None
    expires_at: datetime | None = None  # UTC; None where the provider tokens don't expire
    external_account_id: str | None = None  # workspace/team/installation id at the provider
    scopes: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)  # full token response, for provider-specific extras


@dataclass(frozen=True)
class ChannelRef:
    """One ingestible container at the provider (page/space/repo/project/channel)."""

    external_id: str
    name: str


@dataclass(frozen=True)
class RawItem:
    """A single fetched item before normalization (provider-shaped payload + its id)."""

    external_id: str
    payload: dict


@dataclass(frozen=True)
class RawEvent:
    """Canonical event envelope handed to the extraction pipeline.

    ``raw`` preserves the full original payload so the pipeline can reprocess
    without re-ingesting.
    """

    provider: str
    source_id: str  # provider's id for the item (page id, issue id, …)
    external_event_id: str  # stable dedupe key — unique per (workspace, provider)
    event_type: str  # message | issue | ticket | page | pr | comment
    actor: dict  # {id, email, name} — normalized, best-effort
    content: str  # plain text, normalized from blocks/mrkdwn/ADF/MD
    created_at: datetime  # always UTC
    url: str  # deep link back to the source
    raw: dict


@runtime_checkable
class SourceIntegration(Protocol):
    """Per-provider OAuth + read-only fetch contract."""

    provider: str

    def authorize_url(self, state: str, redirect_uri: str) -> str:
        """Build the provider consent URL for the start of the OAuth flow."""
        ...

    async def exchange_code(self, code: str, redirect_uri: str) -> OAuthTokens:
        """Exchange an authorization ``code`` for tokens."""
        ...

    async def refresh(self, refresh_token: str) -> OAuthTokens:
        """Refresh an access token. Raises ``NotImplementedError`` where N/A."""
        ...

    async def revoke(self, access_token: str) -> None:
        """Best-effort token revocation on disconnect. No-op where unsupported."""
        ...

    async def list_channels(self, access_token: str) -> list[ChannelRef]:
        """List ingestible containers the token can see."""
        ...

    async def fetch_since(
        self,
        access_token: str,
        channel: ChannelRef,
        cursor: str | None,
    ) -> tuple[list[RawItem], str | None]:
        """Fetch items changed since ``cursor``. Returns (items, next_cursor)."""
        ...

    def verify_webhook(self, headers: Mapping[str, str], raw_body: bytes, secret: str) -> bool:
        """Verify a webhook signature against the raw request body."""
        ...

    def normalize(self, item: RawItem) -> RawEvent:
        """Map a provider-shaped item to the canonical :class:`RawEvent`."""
        ...
