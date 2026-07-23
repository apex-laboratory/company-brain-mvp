"""Provider user-directory lookups: id → display name (BRAIN_CHAT_RAG_PLAN dec. F).

Evidence records whoever the extractor saw as the source author. Slack and Zendesk
give a raw id (``U07A3B12`` / ``380288``); this resolves those to a human name via
each provider's users API using the workspace's connection token, so "whose message
said we need this" is a name, not an id. Best-effort by design: any failure (revoked
scope, deleted user, outage) leaves the id in place rather than inventing a name —
an honest id beats a fabricated person.

GitHub/Drive/Gmail/Jira already record readable names, so they need no lookup; Notion
records no message author (a page, not a person), so there is nothing to resolve.
"""
from __future__ import annotations

import logging

from app.integrations.base import http_client

log = logging.getLogger(__name__)

_SLACK_USER_INFO = "https://slack.com/api/users.info"


async def resolve_users(
    provider: str, token: str, ids: list[str], *, subdomain: str | None = None
) -> dict[str, str]:
    """Map ``ids`` → display names for ``provider`` (unresolved ids omitted).

    ``subdomain`` is required for Zendesk (its API host). Never raises — a lookup
    failure returns whatever resolved so far (possibly empty). Network I/O, so call
    OUTSIDE any open transaction.
    """
    unique = [i for i in dict.fromkeys(str(x) for x in ids) if i]
    if not unique or not token:
        return {}
    try:
        if provider == "slack":
            return await _resolve_slack(token, unique)
        if provider == "zendesk":
            return await _resolve_zendesk(token, subdomain, unique)
    except Exception:  # noqa: BLE001 — resolution is best-effort; keep the raw id
        log.warning("user_directory: %s resolution failed", provider, exc_info=True)
    return {}


async def _resolve_slack(token: str, ids: list[str]) -> dict[str, str]:
    """``users.info`` per id (``users:read`` is already granted). Per-id failures are
    skipped so one deleted user doesn't drop the rest."""
    out: dict[str, str] = {}
    headers = {"Authorization": f"Bearer {token}"}
    for uid in ids:
        try:
            resp = await http_client().get(_SLACK_USER_INFO, headers=headers,
                                           params={"user": uid})
            resp.raise_for_status()
            data = resp.json()
        except Exception:  # noqa: BLE001 — skip this id, keep going
            continue
        if not data.get("ok"):
            continue
        user = data.get("user") or {}
        profile = user.get("profile") or {}
        name = (
            user.get("real_name") or profile.get("real_name")
            or profile.get("display_name") or user.get("name")
        )
        if name:
            out[uid] = name
    return out


async def _resolve_zendesk(
    token: str, subdomain: str | None, ids: list[str]
) -> dict[str, str]:
    """``users/show_many`` — one bulk call for all ids on the customer's subdomain."""
    if not subdomain:
        return {}
    resp = await http_client().get(
        f"https://{subdomain}.zendesk.com/api/v2/users/show_many.json",
        headers={"Authorization": f"Bearer {token}"},
        params={"ids": ",".join(ids)},
    )
    resp.raise_for_status()
    return {
        str(u["id"]): u["name"]
        for u in resp.json().get("users", [])
        if u.get("id") is not None and u.get("name")
    }
