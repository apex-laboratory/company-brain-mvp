"""Slack integration — OAuth v2 + polling + webhooks (Slack connector).

Slack is an OAuth 2.0 (v2) bot-token provider with both polling and webhook
ingestion:

* **OAuth:** the v2 flow returns a bot token (``access_token``,
  ``token_type: "bot"``) plus the installing workspace under ``team.id``. We store
  ``team.id`` as the connection's ``external_account_id`` — webhook deliveries are
  routed back to the connection by it (``webhook_ingest`` matches the envelope's
  ``team_id``). Bot tokens do not expire unless token rotation is enabled (we do
  not opt in), so ``refresh`` is unsupported.

* **Polling:** ``conversations.list`` enumerates channels and
  ``conversations.history`` pulls messages per channel. ``source_sync``'s connection
  cursor is ISO-8601 (``last_synced_at``); ``fetch_since`` converts it to a Slack
  ``ts`` for the ``oldest`` param and converts the newest ``ts`` seen back to ISO-8601
  for the returned cursor. ``fetch_since`` enumerates the workspace's own channels
  rather than using the synthetic ``source_sync`` channel as a container, but reads
  that channel's ``external_id`` (the connection's team id) to build message deep
  links. Per-channel errors (e.g. ``not_in_channel``) are isolated so one unreadable
  channel can't fail the whole sweep. Slack rate-limits
  ``conversations.history`` (HTTP 429 + ``Retry-After``); requests honor the header
  for a bounded number of retries, then hold the connection cursor for that channel
  so the next sweep retries rather than advancing past unfetched messages.

* **Webhooks:** Slack's Events API signs each delivery ``v0=<hmac>`` over
  ``v0:{timestamp}:{body}`` (HMAC-SHA256, signing secret), with a 5-minute replay
  window. The app-level signing secret is resolved by the webhooks receiver via
  ``_secret_for("slack")``.

``normalize`` accepts both shapes: the Events API envelope
(``{type: "event_callback", team_id, event: {...}}``) delivered by
``webhook_ingest``, and the bare message object produced by polling (with the
channel id and team id injected by ``fetch_since``, which ``conversations.history``
omits — the team is needed to build a working deep link).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from urllib.parse import urlencode

import httpx

from app.config.settings import settings
from app.integrations.base import (
    ChannelRef,
    ConnectorAuthError,
    OAuthTokens,
    RawEvent,
    RawItem,
    http_client,
)
from app.shared.helpers.crypto import constant_time_compare

log = logging.getLogger(__name__)

_AUTH_URL = "https://slack.com/oauth/v2/authorize"
_TOKEN_URL = "https://slack.com/api/oauth.v2.access"
_API_BASE = "https://slack.com/api"

# Bot scopes: list channels, read public-channel history, resolve user ids.
_SCOPES = "channels:read,channels:history,users:read"
_PAGE_LIMIT = 200  # Slack allows up to 999/1000; 200 keeps payloads modest.
_REPLAY_WINDOW = 300  # seconds; reject Events API deliveries older than 5 minutes.

# conversations.history carries the stricter post-2025 non-Marketplace limits, so 429s
# are expected in real polling. Slack returns HTTP 429 + Retry-After (seconds); honor it
# for a bounded number of retries, then treat the channel as transiently failed (the
# sweep holds the connection cursor and retries next run rather than advancing past it).
_MAX_RATE_LIMIT_RETRIES = 2
_MAX_RETRY_AFTER = 30  # cap a single honored Retry-After sleep (seconds)

# ``ok: false`` errors that mean the whole connection is broken (re-auth needed), not
# just one channel. ``source_sync`` marks the connection ``error`` when these surface.
_AUTH_ERRORS = frozenset(
    {"invalid_auth", "account_inactive", "token_revoked", "not_authed", "token_expired"}
)

# Message subtypes that carry no human-authored content worth ingesting.
_SKIP_SUBTYPES = frozenset(
    {
        "bot_message",
        "message_deleted",
        "message_changed",
        "channel_join",
        "channel_leave",
        "channel_topic",
        "channel_purpose",
        "channel_name",
    }
)


class SlackAPIError(RuntimeError):
    """A Slack Web API failure (HTTP 429 or an ``ok: false`` response).

    ``auth`` failures are whole-connection (the token is broken; re-auth needed).
    ``transient`` failures (rate limits) should hold the cursor and retry next sweep.
    Everything else (e.g. ``not_in_channel``) is a per-channel skip. Subclasses
    ``RuntimeError`` so existing ``except RuntimeError`` callers still catch it.
    """

    def __init__(self, error: str, *, transient: bool = False, auth: bool = False) -> None:
        super().__init__(error)
        self.error = error
        self.transient = transient
        self.auth = auth


def _retry_after_seconds(resp: httpx.Response) -> int:
    """Read Slack's ``Retry-After`` (seconds), clamped to ``[1, _MAX_RETRY_AFTER]``."""
    try:
        return max(1, min(int(resp.headers.get("Retry-After", "1")), _MAX_RETRY_AFTER))
    except (TypeError, ValueError):
        return 1


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive header lookup (Starlette Headers are already insensitive;
    a plain ``dict`` in tests is not)."""
    if name in headers:
        return headers[name]
    lower = name.lower()
    for key, value in headers.items():
        if key.lower() == lower:
            return value
    return None


def _parse_ts(ts: str | None) -> datetime | None:
    """Parse a Slack ``ts`` (``"1700000000.001234"``) into an aware UTC datetime."""
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(float(ts), tz=UTC)
    except (TypeError, ValueError):
        return None


def _iso_to_ts(cursor: str | None) -> str | None:
    """Convert ``source_sync``'s ISO-8601 cursor to a Slack ``ts`` (unix seconds).

    ``source_sync`` stores the connection cursor as ``last_synced_at`` and passes it in
    as ``last_synced_at.isoformat()``; Slack's ``conversations.history`` ``oldest`` param
    expects a ``ts``. Returns ``None`` when there is no cursor (first sweep). A naive
    timestamp is treated as UTC so ``.timestamp()`` can't drift with the host's timezone.
    """
    if not cursor:
        return None
    parsed = datetime.fromisoformat(cursor)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return f"{parsed.timestamp():.6f}"


def _ts_to_iso(ts: str) -> str:
    """Convert a Slack ``ts`` back to an ISO-8601 cursor for ``source_sync``.

    The connection cursor must be ISO-8601: ``source_sync`` re-parses the returned value
    with ``datetime.fromisoformat`` (``_parse_iso``), which would raise on a raw ``ts``.
    """
    return datetime.fromtimestamp(float(ts), tz=UTC).isoformat()


def _is_message(event: dict) -> bool:
    """True when ``event`` is a human-authored channel message worth ingesting."""
    return (
        event.get("type") == "message"
        and event.get("subtype") not in _SKIP_SUBTYPES
        and not event.get("bot_id")
    )


class SlackIntegration:
    """Slack channels + messages integration (OAuth v2 bot token)."""

    provider = "slack"
    # A Slack thread is a discussion → the decision_identifier runs its LLM pass.
    threaded = True
    # ``source_sync`` passes the onboarding picker's selection as
    # ``allowed_channels`` so unselected channels are never fetched.
    supports_channel_filter = True

    def _headers(self, access_token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {access_token}"}

    async def _request(
        self, url: str, access_token: str, params: dict[str, object]
    ) -> dict:
        """GET a Slack Web API method, honoring rate limits and ``ok: false``.

        On HTTP 429 we honor ``Retry-After`` for a bounded number of retries, then
        raise a *transient* :class:`SlackAPIError` so the caller holds the cursor.
        Other HTTP errors (5xx) surface as ``httpx.HTTPStatusError``. An ``ok: false``
        body raises :class:`SlackAPIError` (flagged ``auth`` for re-auth-class errors).
        """
        for attempt in range(_MAX_RATE_LIMIT_RETRIES + 1):
            resp = await http_client().get(
                url, headers=self._headers(access_token), params=params
            )
            if resp.status_code == 429:
                wait = _retry_after_seconds(resp)
                if attempt < _MAX_RATE_LIMIT_RETRIES:
                    log.warning(
                        "slack: 429 on %s; honoring Retry-After=%ss (attempt %d/%d)",
                        url, wait, attempt + 1, _MAX_RATE_LIMIT_RETRIES,
                    )
                    await asyncio.sleep(wait)
                    continue
                raise SlackAPIError("rate_limited", transient=True)
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                err = data.get("error", "unknown_error")
                raise SlackAPIError(err, auth=err in _AUTH_ERRORS)
            return data
        raise SlackAPIError("rate_limited", transient=True)  # retries exhausted

    # ── OAuth ────────────────────────────────────────────────────────────────────
    def authorize_url(
        self, state: str, redirect_uri: str, *, config: Mapping[str, str] | None = None
    ) -> str:
        """Build the Slack v2 consent URL (bot scopes via the ``scope`` param).

        ``config`` is unused — Slack has a global authorize endpoint (the param exists
        only to satisfy the SourceIntegration protocol).
        """
        query = urlencode(
            {
                "client_id": settings.slack_client_id,
                "scope": _SCOPES,
                "redirect_uri": redirect_uri,
                "state": state,
            }
        )
        return f"{_AUTH_URL}?{query}"

    async def exchange_code(
        self, code: str, redirect_uri: str, installation_id: str | None = None
    ) -> OAuthTokens:
        """Exchange an authorization ``code`` for a bot token via oauth.v2.access.

        ``installation_id`` is unused — Slack is a pure code-exchange provider (the
        param exists only to satisfy the SourceIntegration protocol). Slack returns
        HTTP 200 even on failure, signalling errors with ``ok: false``.
        """
        resp = await http_client().post(
            _TOKEN_URL,
            data={
                "client_id": settings.slack_client_id,
                "client_secret": settings.slack_client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise ValueError(f"Slack OAuth failed: {data.get('error')}")

        team = data.get("team") or {}
        scope = data.get("scope") or ""
        return OAuthTokens(
            access_token=data["access_token"],
            refresh_token=None,  # bot tokens don't expire unless rotation is enabled
            expires_at=None,
            external_account_id=team.get("id"),  # routes webhook deliveries
            scopes=scope.split(",") if scope else [],
            raw=data,
        )

    async def refresh(self, refresh_token: str) -> OAuthTokens:
        raise NotImplementedError(
            "Slack bot tokens do not expire (token rotation is not enabled)"
        )

    async def revoke(self, access_token: str) -> None:
        """Best-effort token revocation via auth.revoke; disconnect is local-only."""
        try:
            await http_client().post(
                f"{_API_BASE}/auth.revoke", headers=self._headers(access_token)
            )
        except httpx.HTTPError:
            log.warning("slack revoke: auth.revoke failed; disconnecting locally")
        return None

    # ── fetch ────────────────────────────────────────────────────────────────────
    async def list_channels(self, access_token: str) -> list[ChannelRef]:
        """List the workspace's non-archived public channels (cursor-paginated)."""
        channels: list[ChannelRef] = []
        cursor: str | None = None
        while True:
            params: dict[str, object] = {
                "limit": _PAGE_LIMIT,
                "exclude_archived": "true",
                "types": "public_channel",
            }
            if cursor:
                params["cursor"] = cursor
            data = await self._request(f"{_API_BASE}/conversations.list", access_token, params)
            for conv in data.get("channels", []):
                channels.append(
                    ChannelRef(external_id=conv["id"], name=conv.get("name", conv["id"]))
                )
            cursor = (data.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
        return channels

    async def _history(
        self, access_token: str, channel_id: str, oldest: str | None, team: str
    ) -> tuple[list[RawItem], str | None]:
        """Page through one channel's history after ``oldest`` (a Slack ``ts``).

        Returns ``(items, newest_ts)``. Skips non-content subtypes and bot messages.
        Raises :class:`SlackAPIError` on a Slack ``ok: false`` (or exhausted 429s) so
        ``fetch_since`` can classify the failure — per-channel skip (``not_in_channel``),
        whole-connection auth, or transient rate limit. Injects the channel id **and the
        team id** into each message payload, which ``conversations.history`` omits but
        ``normalize`` needs (the team is required to build a working deep link).
        """
        items: list[RawItem] = []
        newest = oldest
        cursor: str | None = None
        while True:
            params: dict[str, object] = {"channel": channel_id, "limit": _PAGE_LIMIT}
            if oldest:
                params["oldest"] = oldest
            if cursor:
                params["cursor"] = cursor
            data = await self._request(
                f"{_API_BASE}/conversations.history", access_token, params
            )

            for msg in data.get("messages", []):
                if not _is_message(msg):
                    continue
                ts = msg.get("ts")
                if not ts:
                    continue
                msg["channel"] = channel_id  # inject routing context normalize needs
                if team:
                    msg["team"] = team  # conversations.history omits team; deep link needs it
                items.append(RawItem(external_id=ts, payload=msg))
                if newest is None or float(ts) > float(newest):
                    newest = ts

            if not data.get("has_more"):
                break
            cursor = (data.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
        return items, newest

    async def fetch_since(
        self,
        access_token: str,
        channel: ChannelRef,
        cursor: str | None,
        allowed_channels: set[str] | None = None,
    ) -> tuple[list[RawItem], str | None]:
        """Fetch new messages across readable channels since ``cursor``.

        ``allowed_channels`` restricts the fetch to the given channel ids (the
        onboarding picker's selection); ``None`` means all readable channels.

        ``cursor`` is an ISO-8601 timestamp (``source_sync``'s ``last_synced_at``). It is
        converted to a Slack ``ts`` for ``conversations.history``'s ``oldest`` param, and
        the returned next cursor is converted back to ISO-8601 so ``source_sync`` can
        re-parse (``datetime.fromisoformat``) and store it. ``channel`` is the synthetic
        connection-level channel from ``source_sync``: its ``external_id`` is the
        connection's team id (injected into polled messages for deep links). Slack
        enumerates its own channels rather than using it as a container.

        Failures are classified like the GitHub connector so one bad channel can't fail
        the sweep, and the cursor is never advanced past data we didn't fetch:

        * **auth** (``invalid_auth`` / ``token_revoked`` …) — raised as
          :class:`ConnectorAuthError` so ``source_sync`` marks the connection ``error``
          (re-auth needed); Slack reports these as ``ok: false``, not an HTTP 401.
        * **transient** (HTTP 429 after honored retries, or a 5xx) — the channel is held:
          ``incomplete`` keeps the cursor where it was so the next sweep retries it.
        * **per-channel** (``not_in_channel`` …) — skipped permanently; the cursor still
          advances. Dedupe is handled downstream by the ``source_events`` constraint.
        """
        oldest = _iso_to_ts(cursor)  # ISO cursor -> Slack ts for `oldest`
        team = channel.external_id  # connection team id, for message deep links
        items: list[RawItem] = []
        newest_ts = oldest
        incomplete = False  # a transient failure means "don't advance the cursor"

        try:
            channels = await self.list_channels(access_token)
        except SlackAPIError as exc:
            if exc.auth:
                raise ConnectorAuthError(exc.error) from exc
            log.warning("slack fetch: conversations.list failed (%s); holding cursor", exc.error)
            return [], cursor
        except httpx.HTTPStatusError as exc:
            log.warning(
                "slack fetch: conversations.list HTTP %s; holding cursor",
                exc.response.status_code,
            )
            return [], cursor

        for ch in channels:
            if allowed_channels is not None and ch.external_id not in allowed_channels:
                continue
            try:
                ch_items, ch_newest = await self._history(
                    access_token, ch.external_id, oldest, team
                )
            except SlackAPIError as exc:
                if exc.auth:
                    raise ConnectorAuthError(exc.error) from exc
                if exc.transient:
                    log.warning(
                        "slack fetch: rate limited on %s — will retry next sweep", ch.external_id
                    )
                    incomplete = True
                    continue
                log.warning("slack fetch: skipping channel %s (%s)", ch.external_id, exc.error)
                continue
            except httpx.HTTPStatusError as exc:
                if 500 <= exc.response.status_code < 600:
                    log.warning(
                        "slack fetch: server error on %s (%s) — will retry",
                        ch.external_id, exc.response.status_code,
                    )
                    incomplete = True
                    continue
                raise
            items.extend(ch_items)
            if ch_newest and (newest_ts is None or float(ch_newest) > float(newest_ts)):
                newest_ts = ch_newest

        if incomplete or newest_ts is None or newest_ts == oldest:
            return items, cursor  # hold the ISO cursor; nothing newer to advance to
        return items, _ts_to_iso(newest_ts)  # advance as ISO for source_sync

    # ── webhooks ───────────────────────────────────────────────────────────────
    def verify_webhook(
        self, headers: Mapping[str, str], raw_body: bytes, secret: str
    ) -> bool:
        """Verify a Slack Events API signature over the raw request body.

        Slack signs ``v0:{timestamp}:{body}`` with HMAC-SHA256 and sends the result
        as ``X-Slack-Signature: v0=<hex>``. Deliveries older than the replay window
        are rejected even if the signature matches.
        """
        signature = _header(headers, "X-Slack-Signature")
        timestamp = _header(headers, "X-Slack-Request-Timestamp")
        if not signature or not timestamp or not secret:
            return False
        try:
            ts = int(timestamp)
        except (TypeError, ValueError):
            return False
        if abs(int(time.time()) - ts) > _REPLAY_WINDOW:
            return False

        base = b"v0:" + timestamp.encode() + b":" + raw_body
        expected = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
        return constant_time_compare(expected.encode(), signature.encode())

    # ── normalize ────────────────────────────────────────────────────────────────
    def normalize(self, item: RawItem) -> RawEvent:
        """Map a Slack message to the canonical :class:`RawEvent`.

        Accepts both the Events API envelope (``{type: "event_callback", event}``)
        from ``webhook_ingest`` and the bare polled message from ``fetch_since``.
        Raises ``ValueError`` for anything that isn't a human-authored message so
        ``webhook_ingest`` skips it (URL-verification, bot chatter, edits, …).
        """
        payload = item.payload
        if payload.get("type") == "event_callback":
            event = payload.get("event") or {}
            team = payload.get("team_id", "")
        else:
            event = payload
            team = payload.get("team", "")

        if not _is_message(event):
            raise ValueError("Unsupported Slack event (not a user message)")

        ts = event.get("ts")
        channel = event.get("channel", "")
        if not ts or not channel:
            raise ValueError("Slack message missing ts or channel")

        return RawEvent(
            provider="slack",
            source_id=ts,
            # Dedupe key: channel + ts uniquely identify a message in a workspace.
            external_event_id=f"slack:{channel}:{ts}",
            event_type="message",
            actor={"id": event.get("user", ""), "email": "", "name": ""},
            content=event.get("text", ""),
            created_at=_parse_ts(ts) or datetime.now(UTC),
            url=f"https://app.slack.com/client/{team or 'T0'}/{channel}/p{ts.replace('.', '')}",
            raw=payload,
        )
