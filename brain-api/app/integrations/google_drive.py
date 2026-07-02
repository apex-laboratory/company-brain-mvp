"""Google Drive integration — OAuth + polling + push (KAN-2).

Drive's incremental sync is the **Changes** feed, whose cursor is an opaque
``pageToken`` (not a timestamp) — so this connector sets ``opaque_cursor = True`` and
``source_sync`` stores the token in ``source_connections.sync_cursor``.

* **Bootstrap (cursor is None):** capture a start page token, then backfill recent
  files via ``files.list`` (bounded by ``_BOOTSTRAP_LOOKBACK_DAYS``). Return the start
  token as the next cursor so the next sweep is incremental.
* **Incremental (cursor set):** ``changes.list(pageToken=cursor)`` paginates the
  change feed; the last page yields ``newStartPageToken`` (the next cursor).
* **Content:** Google Docs/Sheets/Slides are exported to text via ``files.export``;
  ``text/*`` files are downloaded via ``alt=media``; binaries (images, PDF, …) are
  skipped. Extracted text is stashed on ``payload["_content"]`` for ``normalize``.
* **Push:** ``changes.watch`` posts content-free pings to ``/webhooks/google_drive``
  carrying ``X-Goog-Channel-Token``; ``verify_webhook`` compares it to the
  per-connection token. The receiver then triggers a ``source_sync`` (see webhooks).

Auth failures surface as ``httpx.HTTPStatusError`` (401/403) via ``raise_for_status``
so ``source_sync`` flips the connection to ``error`` (re-auth needed).
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

import httpx

from app.integrations import google_common
from app.integrations.base import ChannelRef, OAuthTokens, RawEvent, RawItem
from app.shared.helpers.crypto import constant_time_compare

log = logging.getLogger(__name__)

_API_BASE = "https://www.googleapis.com/drive/v3"
_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
_PAGE_SIZE = 100
_BOOTSTRAP_LOOKBACK_DAYS = 90  # backfill window for the first sweep
_MAX_CONTENT_BYTES = 5 * 1024 * 1024  # skip downloading binaries/text larger than this

_FILE_FIELDS = (
    "id,name,mimeType,modifiedTime,webViewLink,size,trashed,"
    "owners(displayName,emailAddress)"
)
# Google-native types → the export MIME type we pull plain text from.
_EXPORT_MIME = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _header(headers: Mapping[str, str], name: str) -> str | None:
    if name in headers:
        return headers[name]
    lower = name.lower()
    for key, value in headers.items():
        if key.lower() == lower:
            return value
    return None


class GoogleDriveIntegration:
    provider = "google_drive"
    opaque_cursor = True  # cursor is a Drive changes pageToken, not a timestamp

    def _headers(self, access_token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {access_token}"}

    # ── OAuth (delegated to the shared Google helper) ──────────────────────────────
    def authorize_url(self, state: str, redirect_uri: str) -> str:
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
        # Drive sync is account-wide in this cut (no per-folder selection UI yet).
        return []

    async def _start_page_token(self, access_token: str) -> str:
        resp = await google_common.api_request(
            "GET",
            f"{_API_BASE}/changes/startPageToken",
            headers=self._headers(access_token),
        )
        return resp.json()["startPageToken"]

    async def _content(self, access_token: str, file: dict) -> str:
        """Extract plain text for a file; '' for binaries or on a per-file error."""
        mime = file.get("mimeType", "")
        file_id = file["id"]
        try:
            if mime in _EXPORT_MIME:
                resp = await google_common.api_request(
                    "GET",
                    f"{_API_BASE}/files/{file_id}/export",
                    headers=self._headers(access_token),
                    params={"mimeType": _EXPORT_MIME[mime]},
                )
                return resp.text
            if mime.startswith("text/"):
                size = int(file.get("size") or 0)
                if size and size > _MAX_CONTENT_BYTES:
                    return ""
                resp = await google_common.api_request(
                    "GET",
                    f"{_API_BASE}/files/{file_id}",
                    headers=self._headers(access_token),
                    params={"alt": "media"},
                )
                return resp.text
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403):
                raise  # auth failure — let the sweep classify it
            log.warning("drive content: skipping %s (%s)", file_id, exc.response.status_code)
        return ""

    async def _item_for(self, access_token: str, file: dict) -> RawItem:
        file = {**file, "_content": await self._content(access_token, file)}
        return RawItem(external_id=file["id"], payload=file)

    async def _backfill(self, access_token: str, lookback_days: int | None = None) -> list[RawItem]:
        since = (
            datetime.now(UTC) - timedelta(days=lookback_days or _BOOTSTRAP_LOOKBACK_DAYS)
        ).replace(microsecond=0)
        items: list[RawItem] = []
        page_token: str | None = None
        while True:
            params: dict[str, object] = {
                "q": f"modifiedTime > '{since.isoformat()}' and trashed = false",
                "fields": f"nextPageToken, files({_FILE_FIELDS})",
                "pageSize": _PAGE_SIZE,
                "orderBy": "modifiedTime",
            }
            if page_token:
                params["pageToken"] = page_token
            resp = await google_common.api_request(
                "GET", f"{_API_BASE}/files", headers=self._headers(access_token), params=params
            )
            data = resp.json()
            for file in data.get("files", []):
                items.append(await self._item_for(access_token, file))
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return items

    async def fetch_since(
        self,
        access_token: str,
        channel: ChannelRef,
        cursor: str | None,
        lookback_days: int | None = None,
    ) -> tuple[list[RawItem], str | None]:
        """Fetch files changed since the opaque ``cursor`` (a Drive pageToken).

        ``lookback_days`` bounds the bootstrap backfill (the connection's
        onboarding "how far back?"); it is ignored on incremental syncs.
        """
        if cursor is None:
            # Capture the start token first so changes during the backfill aren't lost
            # (they reappear on the next incremental sweep; inserts dedupe).
            start_token = await self._start_page_token(access_token)
            items = await self._backfill(access_token, lookback_days)
            return items, start_token

        items: list[RawItem] = []
        page_token: str | None = cursor
        next_cursor: str | None = cursor
        while True:
            resp = await google_common.api_request(
                "GET",
                f"{_API_BASE}/changes",
                headers=self._headers(access_token),
                params={
                    "pageToken": page_token,
                    "fields": (
                        "newStartPageToken, nextPageToken, "
                        f"changes(removed, fileId, file({_FILE_FIELDS}))"
                    ),
                    "pageSize": _PAGE_SIZE,
                    "includeRemoved": "false",
                },
            )
            data = resp.json()
            for change in data.get("changes", []):
                file = change.get("file")
                if change.get("removed") or not file or file.get("trashed"):
                    continue
                items.append(await self._item_for(access_token, file))
            if data.get("nextPageToken"):
                page_token = data["nextPageToken"]
                continue
            next_cursor = data.get("newStartPageToken") or cursor
            break
        return items, next_cursor

    # ── push channel lifecycle ─────────────────────────────────────────────────────
    async def register_watch(
        self, access_token: str, *, callback_url: str, token: str
    ) -> tuple[str, datetime | None]:
        """Open a Drive changes.watch channel. Returns ``(channel_id, expires_at)``.

        ``channel_id`` (our generated id) is echoed back as ``X-Goog-Channel-ID`` on
        every delivery, so we store it as the subscription ref for routing; ``token``
        comes back as ``X-Goog-Channel-Token`` for verification.
        """
        page_token = await self._start_page_token(access_token)
        channel_id = uuid.uuid4().hex
        resp = await google_common.api_request(
            "POST",
            f"{_API_BASE}/changes/watch",
            headers={**self._headers(access_token), "Content-Type": "application/json"},
            params={"pageToken": page_token},
            json={
                "id": channel_id,
                "type": "web_hook",
                "address": callback_url,
                "token": token,
            },
        )
        return channel_id, google_common.expiry_from_ms(resp.json().get("expiration"))

    # ── webhooks ───────────────────────────────────────────────────────────────────
    def verify_webhook(self, headers: Mapping[str, str], raw_body: bytes, secret: str) -> bool:
        token = _header(headers, "X-Goog-Channel-Token")
        if not token or not secret:
            return False
        return constant_time_compare(token.encode(), secret.encode())

    # ── normalize ──────────────────────────────────────────────────────────────────
    def normalize(self, item: RawItem) -> RawEvent:
        file = item.payload
        owner = (file.get("owners") or [{}])[0]
        modified = file.get("modifiedTime", "")
        name = file.get("name", "")
        content = file.get("_content", "")
        return RawEvent(
            provider="google_drive",
            source_id=file["id"],
            # Dedupe key: file id + modified time so a re-edit produces a fresh event.
            external_event_id=f"{file['id']}:{modified}",
            event_type="file",
            actor={
                "id": owner.get("emailAddress", ""),
                "email": owner.get("emailAddress", ""),
                "name": owner.get("displayName", ""),
            },
            content=f"{name}\n\n{content}".strip(),
            created_at=_parse_ts(modified) or datetime.now(UTC),
            url=file.get("webViewLink", ""),
            raw=file,
        )
