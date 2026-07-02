"""Notion integration — OAuth + polling fetch (KAN-2).

Notion has **no webhook API**, so ``verify_webhook`` always returns ``False`` and
ingestion is polling-only via the ARQ sync job. Access tokens issued to public
integrations do not expire, so ``refresh`` is unsupported.

Incremental sync uses ``last_edited_time`` as the cursor: ``/v1/search`` returns
pages sorted by ``last_edited_time`` descending, and we stop once we cross the
stored cursor. Rate limited to ~3 requests/second per Notion's documented limit.
"""
from __future__ import annotations

import asyncio
import base64
import time
from collections.abc import Mapping
from datetime import datetime

from app.config.settings import settings
from app.integrations.base import (
    ChannelRef,
    OAuthTokens,
    RawEvent,
    RawItem,
    http_client,
)

_AUTH_URL = "https://api.notion.com/v1/oauth/authorize"
_TOKEN_URL = "https://api.notion.com/v1/oauth/token"
_API_BASE = "https://api.notion.com/v1"
_NOTION_VERSION = "2022-06-28"
_MIN_INTERVAL = 1.0 / 3.0  # ~3 req/s


def _parse_ts(value: str | None) -> datetime | None:
    """Parse a Notion ISO-8601 timestamp (``...Z``) into an aware UTC datetime."""
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _rich_text(blocks: list[dict]) -> str:
    """Concatenate ``plain_text`` from a Notion rich-text array."""
    return "".join(rt.get("plain_text", "") for rt in blocks)


def _title_of(page: dict) -> str:
    """Best-effort page title from its properties (the title-typed property)."""
    for prop in (page.get("properties") or {}).values():
        if prop.get("type") == "title":
            return _rich_text(prop.get("title", []))
    return ""


class NotionIntegration:
    provider = "notion"
    # Notion has no webhooks: updates only arrive by polling, so the
    # poll_pull_sources cron enqueues periodic source_sync runs for it.
    push_delivery = False

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._last_call = 0.0

    async def _throttle(self) -> None:
        """Serialize calls to ~3 req/s (Notion's documented rate limit)."""
        async with self._lock:
            wait = _MIN_INTERVAL - (time.monotonic() - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()

    def _headers(self, access_token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {access_token}",
            "Notion-Version": _NOTION_VERSION,
            "Content-Type": "application/json",
        }

    # ── OAuth ──────────────────────────────────────────────────────────────────
    def authorize_url(self, state: str, redirect_uri: str) -> str:
        from urllib.parse import urlencode

        query = urlencode(
            {
                "client_id": settings.notion_client_id,
                "response_type": "code",
                "owner": "user",
                "redirect_uri": redirect_uri,
                "state": state,
            }
        )
        return f"{_AUTH_URL}?{query}"

    async def exchange_code(
        self, code: str, redirect_uri: str, installation_id: str | None = None
    ) -> OAuthTokens:
        # ``installation_id`` is unused — Notion is a pure code-exchange provider.
        # Notion uses HTTP Basic auth (client_id:client_secret) on the token call.
        basic = base64.b64encode(
            f"{settings.notion_client_id}:{settings.notion_client_secret}".encode()
        ).decode()
        await self._throttle()
        resp = await http_client().post(
            _TOKEN_URL,
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/json",
                "Notion-Version": _NOTION_VERSION,
            },
            json={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return OAuthTokens(
            access_token=data["access_token"],
            refresh_token=None,  # public-integration tokens do not expire
            expires_at=None,
            external_account_id=data.get("workspace_id"),
            scopes=[],
            raw=data,
        )

    async def refresh(self, refresh_token: str) -> OAuthTokens:
        raise NotImplementedError("Notion access tokens do not expire")

    async def revoke(self, access_token: str) -> None:
        # Notion exposes no token-revocation endpoint; disconnect is local-only.
        return None

    # ── fetch ────────────────────────────────────────────────────────────────────
    async def list_channels(self, access_token: str) -> list[ChannelRef]:
        """List top-level pages and databases the integration can see."""
        await self._throttle()
        resp = await http_client().post(
            f"{_API_BASE}/search",
            headers=self._headers(access_token),
            json={"page_size": 100},
        )
        resp.raise_for_status()
        channels: list[ChannelRef] = []
        for obj in resp.json().get("results", []):
            name = _title_of(obj) if obj.get("object") == "page" else _rich_text(
                obj.get("title", [])
            )
            channels.append(ChannelRef(external_id=obj["id"], name=name or "(untitled)"))
        return channels

    async def fetch_since(
        self,
        access_token: str,
        channel: ChannelRef,
        cursor: str | None,
    ) -> tuple[list[RawItem], str | None]:
        """Fetch pages edited after ``cursor`` (an ISO ``last_edited_time``).

        Results come back newest-first; we collect until we cross the cursor, then
        advance the cursor to the newest ``last_edited_time`` seen.
        """
        since = _parse_ts(cursor)
        items: list[RawItem] = []
        newest = since
        start_cursor: str | None = None

        while True:
            body: dict = {
                "sort": {"direction": "descending", "timestamp": "last_edited_time"},
                "page_size": 100,
            }
            if start_cursor:
                body["start_cursor"] = start_cursor

            await self._throttle()
            resp = await http_client().post(
                f"{_API_BASE}/search",
                headers=self._headers(access_token),
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()

            stop = False
            for obj in data.get("results", []):
                edited = _parse_ts(obj.get("last_edited_time"))
                if since is not None and edited is not None and edited <= since:
                    stop = True
                    break
                items.append(RawItem(external_id=obj["id"], payload=obj))
                if edited is not None and (newest is None or edited > newest):
                    newest = edited

            if stop or not data.get("has_more"):
                break
            start_cursor = data.get("next_cursor")

        next_cursor = newest.isoformat() if newest else cursor
        return items, next_cursor

    # ── webhooks ───────────────────────────────────────────────────────────────
    def verify_webhook(self, headers: Mapping[str, str], raw_body: bytes, secret: str) -> bool:
        return False  # Notion has no webhook API — polling only

    # ── normalize ────────────────────────────────────────────────────────────────
    def normalize(self, item: RawItem) -> RawEvent:
        page = item.payload
        created = _parse_ts(page.get("last_edited_time")) or _parse_ts(
            page.get("created_time")
        )
        people = page.get("last_edited_by") or page.get("created_by") or {}
        return RawEvent(
            provider="notion",
            source_id=page["id"],
            # Dedupe key: page id + edit time so a re-edit produces a fresh event.
            external_event_id=f"{page['id']}:{page.get('last_edited_time', '')}",
            event_type="page",
            actor={"id": people.get("id", ""), "email": "", "name": ""},
            content=_title_of(page),
            created_at=created or datetime.now().astimezone(),
            url=page.get("url", ""),
            raw=page,
        )
