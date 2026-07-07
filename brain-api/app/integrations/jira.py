"""Jira Cloud integration — OAuth 2.0 (3LO) + polling (Jira connector).

Jira Cloud is an OAuth 2.0 3LO provider reached through Atlassian's global gateway:

* **OAuth:** consent happens at ``auth.atlassian.com`` (not a per-tenant host — unlike
  Zendesk there is no subdomain to thread). The token exchange returns an access token
  plus a **rotating** refresh token (a new refresh token each time it is used;
  ``source_sync`` persists the rotated one). Which Jira *site(s)* the token can reach is
  discovered **after** exchange via ``GET /oauth/token/accessible-resources``; each site's
  ``id`` is its **cloudId**, and API calls go to
  ``https://api.atlassian.com/ex/jira/{cloudId}/...``. We store the first accessible
  site's cloudId as the connection's ``external_account_id`` (a token can span several
  sites; ``fetch_since``/``list_channels`` re-enumerate them, so the stored id is only a
  reference/webhook-routing anchor).

* **Polling (no webhooks in v1):** Jira has no push in this connector — updates arrive
  by polling, so ``push_delivery = False`` opts the connection into the poll cron and
  ``verify_webhook`` returns ``False``. (Jira *dynamic* webhooks are per-connection REST
  registrations that expire every 30 days and need a public callback URL; deferred.)

* **Multi-container:** a Jira "channel" is a **project**, flattened across every site the
  token can see (``external_id = "{cloudId}:{projectId}"``, name ``"{Site} / {Project}"``).
  ``fetch_since`` groups the onboarding picker's selection back by cloudId and runs one
  JQL search per site, isolating per-site errors like the GitHub/Slack connectors.

* **Incremental fetch:** the new ``POST /rest/api/3/search/jql`` (the old ``/search`` with
  ``startAt``/``total`` is deprecated) paginated by ``nextPageToken``/``isLast``. The
  connection cursor is ISO-8601 (``source_sync``'s ``last_synced_at``); JQL filters on
  ``updated``, whose date literal is **minute-precision in the token user's timezone** —
  so ``fetch_since`` reads that timezone from ``/myself`` and formats the cursor there,
  floored to the minute (inclusive ``>=`` re-fetches the boundary minute; the
  ``source_events`` unique constraint dedupes). The returned next cursor is the newest
  ``fields.updated`` seen, back in ISO-8601 for ``source_sync`` to re-parse.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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

log = logging.getLogger(__name__)

_AUTH_URL = "https://auth.atlassian.com/authorize"
_TOKEN_URL = "https://auth.atlassian.com/oauth/token"
_RESOURCES_URL = "https://api.atlassian.com/oauth/token/accessible-resources"
_API_GATEWAY = "https://api.atlassian.com/ex/jira"

# Classic scopes: read issues/projects/JQL search, read the current user (for /myself
# timezone), and offline_access for the rotating refresh token.
_SCOPES = "read:jira-work read:jira-user offline_access"
_PAGE_SIZE = 100
# Issue fields we pull; ``*navigable`` would be heavier and we only normalize these.
_FIELDS = [
    "summary",
    "description",
    "status",
    "issuetype",
    "priority",
    "created",
    "updated",
    "creator",
    "reporter",
    "assignee",
    "project",
]


def _parse_ts(value: str | None) -> datetime | None:
    """Parse a Jira ISO-8601 timestamp into an aware UTC datetime.

    Jira returns offsets without a colon (``2026-07-02T14:19:16.123-0700``); Python
    3.11+ ``fromisoformat`` accepts that. A naive value is treated as UTC so downstream
    ``.timestamp()``/comparison never drifts with the host timezone.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _adf_to_text(node: object) -> str:
    """Flatten an Atlassian Document Format (ADF) tree to plain text.

    ADF is the JSON body format for v3 issue descriptions/comments: a tree of nodes
    where leaf ``text`` nodes carry the words and ``hardBreak``/``paragraph`` nodes imply
    line breaks. We concatenate text and insert newlines at block boundaries; unknown
    node types are walked transparently so we never drop their children.
    """
    if not isinstance(node, dict):
        return ""
    node_type = node.get("type")
    if node_type == "text":
        return node.get("text", "")
    if node_type == "hardBreak":
        return "\n"
    children = node.get("content")
    if not isinstance(children, list):
        return ""
    text = "".join(_adf_to_text(child) for child in children)
    # Block-level nodes end a line so paragraphs/list items don't run together.
    if node_type in ("paragraph", "heading", "listItem", "blockquote", "codeBlock"):
        return text + "\n"
    return text


def _person(field: object) -> dict[str, str]:
    """Normalize a Jira user object (creator/reporter) to ``{id, email, name}``.

    ``emailAddress`` is usually absent (GDPR profile visibility), so it's best-effort.
    """
    if not isinstance(field, dict):
        return {"id": "", "email": "", "name": ""}
    return {
        "id": field.get("accountId", ""),
        "email": field.get("emailAddress", "") or "",
        "name": field.get("displayName", "") or "",
    }


class JiraIntegration:
    """Jira Cloud issues integration (OAuth 2.0 3LO, polling)."""

    provider = "jira"
    # No webhooks in v1: updates only arrive by polling, so the poll cron enqueues
    # periodic source_sync runs for this connection.
    push_delivery = False
    # A Jira "channel" is a project; source_sync passes the onboarding picker's
    # selection as ``allowed_channels`` so unselected projects are never fetched.
    supports_channel_filter = True

    def _headers(self, access_token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }

    # ── OAuth ──────────────────────────────────────────────────────────────────
    def authorize_url(
        self, state: str, redirect_uri: str, *, config: Mapping[str, str] | None = None
    ) -> str:
        """Build the Atlassian 3LO consent URL.

        ``audience`` + ``prompt=consent`` are required by Atlassian, and
        ``offline_access`` (in the scope) is what yields a refresh token. ``config`` is
        unused — Atlassian's authorize endpoint is global (the cloudId is resolved after
        the token exchange), so the param exists only to satisfy the protocol.
        """
        query = urlencode(
            {
                "audience": "api.atlassian.com",
                "client_id": settings.jira_client_id,
                "scope": _SCOPES,
                "redirect_uri": redirect_uri,
                "state": state,
                "response_type": "code",
                "prompt": "consent",
            }
        )
        return f"{_AUTH_URL}?{query}"

    async def exchange_code(
        self, code: str, redirect_uri: str, installation_id: str | None = None
    ) -> OAuthTokens:
        """Exchange an authorization ``code`` for tokens, then resolve the Jira site.

        ``installation_id`` is unused — Jira is a pure code-exchange provider. After the
        token call we hit ``accessible-resources`` to learn the cloudId(s); the first
        Jira site's cloudId becomes ``external_account_id`` and its name/url ride in
        ``raw`` (``workspace_name`` so the connection is labelled with the site).
        """
        resp = await http_client().post(
            _TOKEN_URL,
            json={
                "grant_type": "authorization_code",
                "client_id": settings.jira_client_id,
                "client_secret": settings.jira_client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
            },
        )
        resp.raise_for_status()
        data = resp.json()

        access_token = data["access_token"]
        sites = await self._accessible_sites(access_token)
        primary = sites[0] if sites else None
        scope = data.get("scope") or ""
        return OAuthTokens(
            access_token=access_token,
            refresh_token=data.get("refresh_token"),
            expires_at=_expiry(data.get("expires_in")),
            external_account_id=primary["id"] if primary else None,
            scopes=scope.split(" ") if scope else [],
            raw={
                "sites": sites,
                "workspace_name": primary["name"] if primary else "Jira",
                **data,
            },
        )

    async def refresh(self, refresh_token: str) -> OAuthTokens:
        """Refresh the access token (rotating refresh grant).

        Atlassian returns a **new** refresh token each call and invalidates the old one,
        so the caller (``source_sync``) must persist ``refresh_token`` from the result.
        """
        resp = await http_client().post(
            _TOKEN_URL,
            json={
                "grant_type": "refresh_token",
                "client_id": settings.jira_client_id,
                "client_secret": settings.jira_client_secret,
                "refresh_token": refresh_token,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        scope = data.get("scope") or ""
        return OAuthTokens(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),  # rotated — persist it
            expires_at=_expiry(data.get("expires_in")),
            scopes=scope.split(" ") if scope else [],
            raw=data,
        )

    async def revoke(self, access_token: str) -> None:
        # Atlassian exposes no simple 3LO token-revocation endpoint; disconnect is
        # local-only (the app can be removed from the site by an admin).
        return None

    async def _accessible_sites(self, access_token: str) -> list[dict]:
        """List the Jira sites this token can reach (``id`` = cloudId).

        ``accessible-resources`` returns every resource (Jira/Confluence) the grant
        covers; we keep those exposing a Jira scope so a Confluence-only site isn't
        treated as a Jira container.
        """
        resp = await http_client().get(_RESOURCES_URL, headers=self._headers(access_token))
        resp.raise_for_status()
        sites: list[dict] = []
        for res in resp.json():
            scopes = res.get("scopes") or []
            if any("jira" in s for s in scopes):
                sites.append(res)
        return sites

    # ── fetch ────────────────────────────────────────────────────────────────────
    async def list_channels(self, access_token: str) -> list[ChannelRef]:
        """List projects across every accessible Jira site (the onboarding picker).

        ``external_id`` encodes both the site and project (``"{cloudId}:{projectId}"``)
        so ``fetch_since`` can route a selected project back to its site's API base.
        """
        channels: list[ChannelRef] = []
        for site in await self._accessible_sites(access_token):
            cloud_id = site["id"]
            site_name = site.get("name", cloud_id)
            base = f"{_API_GATEWAY}/{cloud_id}"
            start_at = 0
            while True:
                resp = await http_client().get(
                    f"{base}/rest/api/3/project/search",
                    headers=self._headers(access_token),
                    params={"startAt": start_at, "maxResults": _PAGE_SIZE},
                )
                resp.raise_for_status()
                data = resp.json()
                for proj in data.get("values", []):
                    channels.append(
                        ChannelRef(
                            external_id=f"{cloud_id}:{proj['id']}",
                            name=f"{site_name} / {proj.get('name', proj['id'])}",
                        )
                    )
                if data.get("isLast", True):
                    break
                start_at += data.get("maxResults", _PAGE_SIZE)
        return channels

    async def _timezone(self, base: str, access_token: str) -> ZoneInfo:
        """Read the token user's timezone (JQL date literals are interpreted in it).

        Falls back to UTC when the profile hides it or the name is unknown; most Cloud
        sites default to UTC, and the inclusive minute-floor cursor tolerates the slack.
        """
        try:
            resp = await http_client().get(
                f"{base}/rest/api/3/myself", headers=self._headers(access_token)
            )
            resp.raise_for_status()
            name = resp.json().get("timeZone") or "UTC"
            return ZoneInfo(name)
        except (httpx.HTTPError, KeyError, ZoneInfoNotFoundError, ValueError):
            return ZoneInfo("UTC")

    async def _search_site(
        self,
        base: str,
        access_token: str,
        jql: str,
        site_url: str,
    ) -> tuple[list[RawItem], datetime | None]:
        """Run one site's JQL search, paging ``nextPageToken`` until ``isLast``.

        Returns ``(items, newest_updated)``. Injects the site's canonical ``site_url``
        (the browsable host, e.g. ``https://acme.atlassian.net``) into each issue for the
        deep link in ``normalize`` — the API gateway base is not browsable. Raises
        ``httpx.HTTPStatusError`` on HTTP failures so ``fetch_since`` can classify them.
        """
        items: list[RawItem] = []
        newest: datetime | None = None
        next_token: str | None = None
        while True:
            body: dict = {"jql": jql, "maxResults": _PAGE_SIZE, "fields": _FIELDS}
            if next_token:
                body["nextPageToken"] = next_token
            resp = await http_client().post(
                f"{base}/rest/api/3/search/jql",
                headers=self._headers(access_token),
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()
            for issue in data.get("issues", []):
                issue["_site_url"] = site_url  # browsable host for the deep link
                items.append(RawItem(external_id=str(issue["id"]), payload=issue))
                updated = _parse_ts((issue.get("fields") or {}).get("updated"))
                if updated and (newest is None or updated > newest):
                    newest = updated
            # The absence of nextPageToken is the authoritative "last page" signal;
            # isLast is not present on every /search/jql response, so don't default it
            # to True (that would stop after page 1 when more pages exist).
            next_token = data.get("nextPageToken")
            if not next_token or data.get("isLast") is True:
                break
        return items, newest

    async def fetch_since(
        self,
        access_token: str,
        channel: ChannelRef,
        cursor: str | None,
        allowed_channels: set[str] | None = None,
    ) -> tuple[list[RawItem], str | None]:
        """Fetch issues updated since ``cursor`` across the selected projects/sites.

        ``allowed_channels`` (``"{cloudId}:{projectId}"`` values) is the onboarding
        picker's selection; ``None`` means every project on every accessible site. The
        selection is grouped by cloudId so each site runs one JQL search filtered to its
        projects.

        ``cursor`` is an ISO-8601 timestamp. Per site it is converted to the user's
        timezone and floored to the minute for the JQL ``updated >=`` bound (inclusive;
        the boundary minute is re-fetched and deduped downstream). The returned next
        cursor is the newest ``updated`` seen, back in ISO-8601 for ``source_sync``.

        Per-site errors are isolated so one unreadable site can't fail the sweep, and the
        cursor is never advanced past data we didn't fetch:

        * **401** (token invalid/revoked) — raised as :class:`ConnectorAuthError` so
          ``source_sync`` marks the connection ``error`` (re-auth needed).
        * **429 / 5xx** — the site is held (``incomplete``): the cursor stays put so the
          next sweep retries rather than skipping issues.
        * **403 / other** — that site is skipped for this sweep; the cursor still advances.
        """
        by_site = _group_by_site(allowed_channels)
        since = _parse_ts(cursor)
        items: list[RawItem] = []
        newest: datetime | None = since
        incomplete = False

        try:
            sites = await self._accessible_sites(access_token)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                raise ConnectorAuthError("jira accessible-resources 401") from exc
            log.warning("jira fetch: accessible-resources HTTP %s; holding cursor",
                        exc.response.status_code)
            return [], cursor

        for site in sites:
            cloud_id = site["id"]
            if by_site is not None and cloud_id not in by_site:
                continue  # no selected project on this site
            base = f"{_API_GATEWAY}/{cloud_id}"
            try:
                tz = await self._timezone(base, access_token)
                jql = _build_jql(since, tz, by_site.get(cloud_id) if by_site else None)
                site_items, site_newest = await self._search_site(
                    base, access_token, jql, site.get("url", "")
                )
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status == 401:
                    raise ConnectorAuthError(f"jira {status} on {cloud_id}") from exc
                if status == 429 or 500 <= status < 600:
                    log.warning("jira fetch: HTTP %s on %s — will retry next sweep",
                                status, cloud_id)
                    incomplete = True
                    continue
                log.warning("jira fetch: skipping site %s (HTTP %s)", cloud_id, status)
                continue
            items.extend(site_items)
            if site_newest and (newest is None or site_newest > newest):
                newest = site_newest

        if incomplete or newest is None or newest == since:
            return items, cursor  # hold the cursor; nothing newer to advance to
        return items, newest.isoformat()

    # ── webhooks ───────────────────────────────────────────────────────────────
    def verify_webhook(self, headers: Mapping[str, str], raw_body: bytes, secret: str) -> bool:
        # Jira dynamic webhooks (per-connection REST registration + 30-day extension)
        # are a follow-up; v1 is polling-only.
        return False

    # ── normalize ────────────────────────────────────────────────────────────────
    def normalize(self, item: RawItem) -> RawEvent:
        """Map a Jira issue to the canonical :class:`RawEvent`.

        ``content`` is the summary followed by the ADF-flattened description; the deep
        link is built from the site base injected by ``fetch_since`` and the issue key.
        """
        issue = item.payload
        fields = issue.get("fields") or {}
        summary = fields.get("summary", "") or ""
        description = _adf_to_text(fields.get("description")).strip()
        content = f"{summary}\n\n{description}".strip() if description else summary

        updated = fields.get("updated", "")
        created = _parse_ts(fields.get("created")) or _parse_ts(updated)
        key = issue.get("key", "")
        site_url = (issue.get("_site_url", "") or "").rstrip("/")

        return RawEvent(
            provider="jira",
            source_id=str(issue.get("id", key)),
            # Dedupe key: issue id + updated time so a later edit yields a fresh event.
            external_event_id=f"jira:{issue.get('id', key)}:{updated}",
            event_type="issue",
            actor=_person(fields.get("creator") or fields.get("reporter")),
            content=content,
            created_at=created or datetime.now(UTC),
            url=f"{site_url}/browse/{key}" if key and site_url else "",
            raw=issue,
        )


def _expiry(expires_in: object) -> datetime | None:
    """UTC expiry from the token response's ``expires_in`` (seconds)."""
    if not isinstance(expires_in, (int, float)):
        return None
    return datetime.now(UTC) + timedelta(seconds=int(expires_in))


def _group_by_site(allowed_channels: set[str] | None) -> dict[str, set[str]] | None:
    """Group ``"{cloudId}:{projectId}"`` selections into ``{cloudId: {projectId}}``.

    ``None`` (no picker selection) stays ``None`` = fetch every project on every site.
    """
    if allowed_channels is None:
        return None
    grouped: dict[str, set[str]] = {}
    for ref in allowed_channels:
        cloud_id, _, project_id = ref.partition(":")
        # Jira project ids are numeric; require digits so a tampered selection value
        # can't inject JQL (the id is interpolated into the `project in (...)` clause).
        if cloud_id and project_id.isdigit():
            grouped.setdefault(cloud_id, set()).add(project_id)
    return grouped


def _build_jql(since: datetime | None, tz: ZoneInfo, project_ids: set[str] | None) -> str:
    """Build ``[project in (...) AND] updated >= "..." ORDER BY updated ASC``.

    The ``updated`` bound is the cursor in the user's timezone, floored to the minute
    (JQL date literals are minute-precision, local-time). ORDER BY ascending so
    pagination walks oldest→newest and the newest ``updated`` is the last page's tail.
    """
    clauses: list[str] = []
    if project_ids:
        clauses.append(f"project in ({','.join(sorted(project_ids))})")
    if since is not None:
        local = since.astimezone(tz)
        clauses.append(f'updated >= "{local.strftime("%Y-%m-%d %H:%M")}"')
    where = " AND ".join(clauses)
    # No `since` (no cursor and no lookback configured) → an unbounded backfill of every
    # issue. source_sync normally seeds the cursor from the connection's lookback window,
    # so an unbounded query is only the deliberate "no lookback" first sync.
    return f"{where} ORDER BY updated ASC" if where else "ORDER BY updated ASC"
