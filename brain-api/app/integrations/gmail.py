"""Gmail integration — OAuth + polling + push (KAN-2).

Gmail's incremental sync is the **History** feed, whose cursor is an opaque
``historyId`` (a monotonically increasing string, not a timestamp) — so this connector
sets ``opaque_cursor = True`` and ``source_sync`` stores it in
``source_connections.sync_cursor``.

* **Bootstrap (cursor is None):** backfill recent messages via ``messages.list``
  (``after:`` bounded by ``_BOOTSTRAP_LOOKBACK_DAYS``), then read the mailbox
  ``historyId`` from ``getProfile`` as the next cursor.
* **Incremental (cursor set):** ``history.list(startHistoryId=cursor,
  historyTypes=messageAdded)`` returns added message ids + the latest ``historyId``.
  A stored id older than Gmail keeps (~1 week–30 days) yields **HTTP 404** → fall back
  to a full bootstrap to re-establish the cursor.
* **Content:** ``messages.get(format=full)``; the MIME tree is walked for ``text/plain``
  (falling back to stripped ``text/html``), base64url-decoded.
* **Push:** ``users.watch`` publishes to Cloud Pub/Sub, which POSTs to
  ``/webhooks/gmail?token=…``; the receiver verifies the token and triggers a
  ``source_sync`` (Gmail push carries no content). So ``verify_webhook`` is unused for
  Gmail (returns ``False``) — verification happens in the webhooks service.

Auth failures surface as ``httpx.HTTPStatusError`` (401/403) so ``source_sync`` flips
the connection to ``error`` (re-auth needed).
"""
from __future__ import annotations

import base64
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

import httpx

from app.config.settings import settings
from app.integrations import google_common
from app.integrations.base import (
    BACKFILL_CURSOR_PREFIX,
    ChannelRef,
    OAuthTokens,
    RawEvent,
    RawItem,
)

_API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
_PAGE_SIZE = 100
_BOOTSTRAP_LOOKBACK_DAYS = 90
# Max messages fetched per bootstrap run before persisting a continuation cursor. Bounds
# memory + wall time (each message is a serial messages.get) so onboarding resumes across
# runs instead of restarting from zero on a job timeout.
_BACKFILL_MAX = 500
_TAG_RE = re.compile(r"<[^>]+>")


def _b64url(data: str | None) -> str:
    if not data:
        return ""
    return base64.urlsafe_b64decode(data + "===").decode("utf-8", errors="replace")


def _header_value(headers: list[dict], name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _message_text(payload: dict) -> str:
    """Depth-first extract of the message body: prefer text/plain, else stripped HTML."""
    mime = payload.get("mimeType", "")
    body = payload.get("body", {})
    if mime == "text/plain" and body.get("data"):
        return _b64url(body["data"])
    if mime == "text/html" and body.get("data"):
        return _TAG_RE.sub("", _b64url(body["data"]))
    for part in payload.get("parts", []) or []:
        text = _message_text(part)
        if text:
            return text
    return ""


class GmailIntegration:
    provider = "gmail"
    opaque_cursor = True  # cursor is a Gmail historyId, not a timestamp

    def _headers(self, access_token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {access_token}"}

    # ── OAuth (delegated to the shared Google helper) ──────────────────────────────
    def authorize_url(
        self, state: str, redirect_uri: str, *, config: Mapping[str, str] | None = None
    ) -> str:
        # ``config`` is unused — Google's authorize endpoint is global.
        return google_common.authorize_url(_SCOPE, state, redirect_uri)

    async def exchange_code(
        self, code: str, redirect_uri: str, installation_id: str | None = None
    ) -> OAuthTokens:
        return await google_common.exchange_code(code, redirect_uri)

    async def refresh(self, refresh_token: str) -> OAuthTokens:
        return await google_common.refresh(refresh_token)

    async def revoke(self, access_token: str) -> None:
        return await google_common.revoke(access_token)

    # ── fetch ──────────────────────────────────────────────────────────────────────
    async def list_channels(self, access_token: str) -> list[ChannelRef]:
        return []

    async def _get_message(self, access_token: str, message_id: str) -> RawItem:
        resp = await google_common.api_request(
            "GET",
            f"{_API_BASE}/messages/{message_id}",
            headers=self._headers(access_token),
            params={"format": "full"},
        )
        return RawItem(external_id=message_id, payload=resp.json())

    async def _profile_history_id(self, access_token: str) -> str:
        resp = await google_common.api_request(
            "GET", f"{_API_BASE}/profile", headers=self._headers(access_token)
        )
        return str(resp.json()["historyId"])

    async def _bootstrap(
        self,
        access_token: str,
        lookback_days: int | None = None,
        *,
        after: int | None = None,
        history_id: str | None = None,
        page_token: str | None = None,
    ) -> tuple[list[RawItem], str]:
        """Backfill recent messages, bounded to ``_BACKFILL_MAX`` per run.

        The mailbox ``historyId`` is captured before the first page (so messages arriving
        during the backfill aren't missed) and carried across chunks. When the per-run cap
        is hit with more to fetch, we return a ``BACKFILL_CURSOR_PREFIX`` continuation
        cursor (``after|historyId|pageToken``); ``source_sync`` chains the next chunk. When
        the mailbox is exhausted we return the plain ``historyId`` and incremental sync
        takes over. Bounding each run keeps memory + wall time in check so a huge mailbox
        can't blow the job timeout and retry from zero.
        """
        if after is None:
            after = int(
                (
                    datetime.now(UTC) - timedelta(days=lookback_days or _BOOTSTRAP_LOOKBACK_DAYS)
                ).timestamp()
            )
        if history_id is None:
            # Read the cursor first so messages arriving during the backfill aren't missed.
            history_id = await self._profile_history_id(access_token)

        items: list[RawItem] = []
        while len(items) < _BACKFILL_MAX:
            params: dict[str, object] = {"q": f"after:{after}", "maxResults": _PAGE_SIZE}
            if page_token:
                params["pageToken"] = page_token
            resp = await google_common.api_request(
                "GET", f"{_API_BASE}/messages", headers=self._headers(access_token), params=params
            )
            data = resp.json()
            for msg in data.get("messages", []):
                items.append(await self._get_message(access_token, msg["id"]))
            page_token = data.get("nextPageToken")
            if not page_token:
                return items, history_id  # backfill complete → hand off to incremental
        # Hit the per-run cap with more pages left — persist a continuation cursor.
        return items, f"{BACKFILL_CURSOR_PREFIX}{after}|{history_id}|{page_token}"

    async def fetch_since(
        self,
        access_token: str,
        channel: ChannelRef,
        cursor: str | None,
        lookback_days: int | None = None,
    ) -> tuple[list[RawItem], str | None]:
        """Fetch messages added since the opaque ``cursor`` (a Gmail historyId).

        ``lookback_days`` bounds the bootstrap backfill (the connection's
        onboarding "how far back?"); it is ignored on incremental syncs.
        """
        if cursor is None:
            return await self._bootstrap(access_token, lookback_days)
        if cursor.startswith(BACKFILL_CURSOR_PREFIX):
            after_s, history_id, page_token = cursor[len(BACKFILL_CURSOR_PREFIX):].split("|", 2)
            return await self._bootstrap(
                access_token, after=int(after_s), history_id=history_id, page_token=page_token
            )

        items: list[RawItem] = []
        seen: set[str] = set()
        newest = cursor
        page_token: str | None = None
        while True:
            params: dict[str, object] = {
                "startHistoryId": cursor,
                "historyTypes": "messageAdded",
                "maxResults": _PAGE_SIZE,
            }
            if page_token:
                params["pageToken"] = page_token
            try:
                resp = await google_common.api_request(
                    "GET", f"{_API_BASE}/history", headers=self._headers(access_token), params=params
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    # Stored historyId is too old — Gmail dropped it. Full resync.
                    return await self._bootstrap(access_token, lookback_days)
                raise
            data = resp.json()
            for record in data.get("history", []):
                for added in record.get("messagesAdded", []):
                    msg_id = added.get("message", {}).get("id")
                    if msg_id and msg_id not in seen:
                        seen.add(msg_id)
                        items.append(await self._get_message(access_token, msg_id))
            if data.get("historyId"):
                newest = str(data["historyId"])
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return items, newest

    # ── push channel lifecycle ─────────────────────────────────────────────────────
    async def register_watch(
        self, access_token: str, *, callback_url: str, token: str
    ) -> tuple[str, datetime | None]:
        """Open a Gmail watch on INBOX → Cloud Pub/Sub. Returns ``(ref, expires_at)``.

        ``callback_url``/``token`` are unused: Gmail push is delivered via the Pub/Sub
        topic + push subscription configured out of band (see .env.example). Routing is
        by the account email (``external_account_id``), so the subscription ref is just
        a marker.
        """
        resp = await google_common.api_request(
            "POST",
            f"{_API_BASE}/watch",
            headers={**self._headers(access_token), "Content-Type": "application/json"},
            json={"topicName": settings.google_pubsub_topic, "labelIds": ["INBOX"]},
        )
        return "gmail-watch", google_common.expiry_from_ms(resp.json().get("expiration"))

    # ── webhooks ───────────────────────────────────────────────────────────────────
    def verify_webhook(self, headers: Mapping[str, str], raw_body: bytes, secret: str) -> bool:
        # Gmail push is a Pub/Sub delivery verified by URL token in the webhooks
        # service (not an HMAC over the body), so this is unused.
        return False

    # ── normalize ──────────────────────────────────────────────────────────────────
    def normalize(self, item: RawItem) -> RawEvent:
        msg = item.payload
        payload = msg.get("payload", {})
        headers = payload.get("headers", [])
        subject = _header_value(headers, "Subject")
        sender = _header_value(headers, "From")
        body = _message_text(payload)

        internal = msg.get("internalDate")
        created = (
            datetime.fromtimestamp(int(internal) / 1000, UTC)
            if internal
            else datetime.now(UTC)
        )
        return RawEvent(
            provider="gmail",
            source_id=msg["id"],
            external_event_id=f"gmail:{msg['id']}",
            event_type="email",
            actor={"id": "", "email": sender, "name": sender},
            content=f"{subject}\n\n{body}".strip(),
            created_at=created,
            url=f"https://mail.google.com/mail/u/0/#all/{msg['id']}",
            raw=msg,
        )
