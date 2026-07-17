"""Zendesk integration — OAuth 2.0 + polling (incremental ticket export).

Zendesk differs from the other connectors in one structural way: it is
**subdomain-scoped**. Every OAuth and API call targets ``{subdomain}.zendesk.com``,
where the subdomain is the customer's own Zendesk instance (``acme.zendesk.com``).
The OAuth *client* is global (one ``client_id`` across all customers), but the
subdomain must be supplied by the connecting workspace and travels through the flow:

* **OAuth:** ``authorize_url`` needs the subdomain up front (it is the URL host), so
  the sources service passes it via ``config={"subdomain": ...}``. At callback the
  subdomain is handed to ``exchange_code`` through the generic ``installation_id``
  carrier and stored as the connection's ``external_account_id``. Access tokens issued
  to a Zendesk OAuth client do not expire (token expiry is opt-in), so ``refresh`` is
  unsupported.

* **Polling:** ``fetch_since`` reads the subdomain from the synthetic connection-level
  ``channel`` that ``source_sync`` builds from ``external_account_id``, then pages the
  **time-based** incremental export
  (``/api/v2/incremental/tickets.json?start_time=``). That endpoint returns an
  ``end_time`` (unix epoch) which we hand back as the next cursor in ISO form — this is
  what fits ``source_sync``'s datetime cursor model (``last_synced_at``). Zendesk
  rate-limits incremental export (~10 req/min, HTTP 429 + ``Retry-After``); requests
  honor the header for a bounded number of retries.

* **Webhooks:** verification is implemented (base64 HMAC-SHA256 over
  ``timestamp + body``) and ready, but registration + per-webhook secret storage is a
  follow-up — this connector ships polling-first, like Notion. Until the receiver
  resolves a Zendesk signing secret, ``verify_webhook`` returns ``False`` (empty
  secret), so no Zendesk webhook is accepted yet.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from urllib.parse import urlencode

from app.config.settings import settings
from app.integrations.base import (
    ChannelRef,
    OAuthTokens,
    RawEvent,
    RawItem,
    http_client,
)
from app.shared.helpers.crypto import constant_time_compare

log = logging.getLogger(__name__)

# Read-only access to tickets and their content.
_SCOPES = "read"
_PAGE_SIZE = 1000  # incremental export returns up to 1000 records per page
_MAX_PAGES = 100  # safety bound per sweep (100k tickets) to avoid runaway loops
_MAX_RATE_LIMIT_RETRIES = 2
_MAX_RETRY_AFTER = 30  # cap a single honored Retry-After sleep (seconds)


def _base_url(subdomain: str) -> str:
    return f"https://{subdomain}.zendesk.com"


def _retry_after_seconds(resp) -> int:
    """Read Zendesk's ``Retry-After`` (seconds), clamped to ``[1, _MAX_RETRY_AFTER]``."""
    try:
        return max(1, min(int(resp.headers.get("Retry-After", "1")), _MAX_RETRY_AFTER))
    except (TypeError, ValueError):
        return 1


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive header lookup (Starlette Headers are insensitive; dicts aren't)."""
    if name in headers:
        return headers[name]
    lower = name.lower()
    for key, value in headers.items():
        if key.lower() == lower:
            return value
    return None


def _parse_ts(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp into an aware UTC datetime.

    Handles both Zendesk's ``...Z`` ticket timestamps and ``source_sync``'s cursor
    (``last_synced_at.isoformat()``). A naive value is treated as UTC so ``.timestamp()``
    on the ``fetch_since`` cursor path can't drift with the host's local timezone.
    """
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


class ZendeskIntegration:
    provider = "zendesk"
    # A ticket's comment chain is a discussion → decision_identifier LLM pass.
    threaded = True
    # Webhook registration is a follow-up (see module docstring), so until it lands
    # updates only arrive by polling — opt this connection into the poll cron.
    push_delivery = False

    def _headers(self, access_token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {access_token}"}

    async def _get(self, url: str, access_token: str, params: dict | None = None) -> dict:
        """GET a Zendesk endpoint, honoring HTTP 429 ``Retry-After`` (bounded retries).

        Zendesk uses standard HTTP status codes, so non-429 errors surface as
        ``httpx.HTTPStatusError`` — ``source_sync`` already classifies 401/403 as auth
        failures (mark connection ``error``) and other statuses as transient (retry).
        """
        for attempt in range(_MAX_RATE_LIMIT_RETRIES + 1):
            resp = await http_client().get(
                url, headers=self._headers(access_token), params=params
            )
            if resp.status_code == 429 and attempt < _MAX_RATE_LIMIT_RETRIES:
                wait = _retry_after_seconds(resp)
                log.warning(
                    "zendesk: 429 on %s; honoring Retry-After=%ss (attempt %d/%d)",
                    url, wait, attempt + 1, _MAX_RATE_LIMIT_RETRIES,
                )
                await asyncio.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError("unreachable")  # loop always returns or raises

    # ── OAuth ──────────────────────────────────────────────────────────────────
    def authorize_url(
        self, state: str, redirect_uri: str, *, config: Mapping[str, str] | None = None
    ) -> str:
        """Build the per-subdomain Zendesk consent URL.

        ``config["subdomain"]`` is required: it is the URL host and is supplied by the
        connecting workspace (Zendesk has no global authorize endpoint).
        """
        subdomain = (config or {}).get("subdomain")
        if not subdomain:
            raise ValueError("Zendesk authorize requires a subdomain")
        query = urlencode(
            {
                "response_type": "code",
                "client_id": settings.zendesk_client_id,
                "scope": _SCOPES,
                "redirect_uri": redirect_uri,
                "state": state,
            }
        )
        return f"{_base_url(subdomain)}/oauth/authorizations/new?{query}"

    async def exchange_code(
        self, code: str, redirect_uri: str, installation_id: str | None = None
    ) -> OAuthTokens:
        """Exchange the authorization ``code`` for a token at the subdomain's endpoint.

        ``installation_id`` carries the subdomain (the generic provider-specific carrier
        in the protocol); it is required and is stored as ``external_account_id`` so the
        sync job can target the right instance.
        """
        subdomain = installation_id
        if not subdomain:
            raise ValueError("Zendesk exchange requires a subdomain")
        resp = await http_client().post(
            f"{_base_url(subdomain)}/oauth/tokens",
            json={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": settings.zendesk_client_id,
                "client_secret": settings.zendesk_client_secret,
                "redirect_uri": redirect_uri,
                "scope": _SCOPES,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        scope = data.get("scope") or ""
        return OAuthTokens(
            access_token=data["access_token"],
            refresh_token=None,  # Zendesk OAuth tokens don't expire (expiry is opt-in)
            expires_at=None,
            external_account_id=subdomain,  # the instance host; feeds fetch_since
            scopes=scope.split() if scope else [],
            raw=data,
        )

    async def refresh(self, refresh_token: str) -> OAuthTokens:
        raise NotImplementedError("Zendesk OAuth tokens do not expire")

    async def revoke(self, access_token: str) -> None:
        # Revoking needs the numeric token id (not stored); disconnect is local-only.
        return None

    # ── fetch ────────────────────────────────────────────────────────────────────
    async def list_channels(self, access_token: str) -> list[ChannelRef]:
        """Zendesk has no sub-containers to select; tickets are ingested as a whole."""
        return [ChannelRef(external_id="tickets", name="All tickets")]

    async def fetch_since(
        self,
        access_token: str,
        channel: ChannelRef,
        cursor: str | None,
    ) -> tuple[list[RawItem], str | None]:
        """Fetch tickets updated since ``cursor`` via the time-based incremental export.

        ``channel.external_id`` is the subdomain (``source_sync`` builds the synthetic
        channel from ``external_account_id``). ``cursor`` is an ISO timestamp; we convert
        it to a unix ``start_time``, page until the stream ends, and return the newest
        ``end_time`` as the next cursor in ISO form. Each ticket payload is tagged with
        the subdomain so ``normalize`` can build a deep link.

        Note: Zendesk's incremental export is **inclusive** of ``start_time``, so the
        boundary ticket(s) at exactly the cursor time re-appear on the next sweep (and on
        a quiet instance the cursor holds at that second until a newer ticket arrives).
        This is expected — the ``source_events`` unique constraint dedupes them, same as
        GitHub's inclusive ``since`` semantics — so it is safe to advance the cursor to
        ``end_time`` and rely on downstream dedup rather than trying to skip the boundary.
        """
        subdomain = channel.external_id
        start_time = int(_parse_ts(cursor).timestamp()) if cursor else 0
        url = f"{_base_url(subdomain)}/api/v2/incremental/tickets.json"

        items: list[RawItem] = []
        newest = start_time
        for _ in range(_MAX_PAGES):
            data = await self._get(url, access_token, params={"start_time": start_time})
            tickets = data.get("tickets", [])
            for ticket in tickets:
                ticket["_subdomain"] = subdomain  # inject for normalize's deep link
                items.append(RawItem(external_id=str(ticket["id"]), payload=ticket))

            end_time = data.get("end_time")
            # Stop at the end of the stream, on a short page, or if time stops advancing.
            if (
                data.get("end_of_stream")
                or len(tickets) < _PAGE_SIZE
                or not end_time
                or end_time <= start_time
            ):
                if end_time and end_time > newest:
                    newest = end_time
                break
            newest = end_time
            start_time = end_time

        next_cursor = datetime.fromtimestamp(newest, tz=UTC).isoformat() if newest else cursor
        return items, next_cursor

    # ── webhooks (verification ready; registration is a follow-up) ───────────────
    def verify_webhook(self, headers: Mapping[str, str], raw_body: bytes, secret: str) -> bool:
        """Verify a Zendesk webhook signature over ``timestamp + body``.

        Zendesk signs ``{timestamp}{body}`` with HMAC-SHA256 (per-webhook secret) and
        base64-encodes it into ``x-zendesk-webhook-signature``. Registration and secret
        storage land with the webhook follow-up, so until a secret is wired this returns
        ``False`` for an empty secret (no Zendesk webhook is accepted yet).
        """
        signature = _header(headers, "x-zendesk-webhook-signature")
        timestamp = _header(headers, "x-zendesk-webhook-signature-timestamp")
        if not signature or not timestamp or not secret:
            return False
        base = timestamp.encode() + raw_body
        expected = base64.b64encode(
            hmac.new(secret.encode(), base, hashlib.sha256).digest()
        )
        return constant_time_compare(expected, signature.encode())

    # ── normalize ────────────────────────────────────────────────────────────────
    def normalize(self, item: RawItem) -> RawEvent:
        """Map a Zendesk ticket to the canonical :class:`RawEvent`."""
        ticket = item.payload
        ticket_id = ticket.get("id")
        if ticket_id is None:
            raise ValueError("Zendesk payload is not a ticket (no id)")

        updated = ticket.get("updated_at") or ticket.get("created_at")
        subject = ticket.get("subject") or ""
        description = ticket.get("description") or ""
        subdomain = ticket.get("_subdomain", "")
        url = (
            f"{_base_url(subdomain)}/agent/tickets/{ticket_id}"
            if subdomain
            else ticket.get("url", "")
        )
        return RawEvent(
            provider="zendesk",
            source_id=str(ticket_id),
            # Dedupe key: ticket id + update time so a re-edit produces a fresh event.
            external_event_id=f"zendesk:{ticket_id}:{updated or ''}",
            event_type="ticket",
            actor={"id": str(ticket.get("requester_id", "")), "email": "", "name": ""},
            content=f"{subject}\n\n{description}".strip(),
            created_at=_parse_ts(updated) or datetime.now(UTC),
            url=url,
            raw=ticket,
        )
