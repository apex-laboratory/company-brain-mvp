"""Skill-cache invalidation on publish (spec: ``services/skill_writer.py``).

The read-side cache (`skills:{workspace_id}:*`) is populated by Phase 5's
``query_brain``; invalidating the key space now keeps every publish/approve
path forward-compatible. Best-effort: a Redis outage must not fail a skill
write (worst case is a stale cache entry until TTL), so errors log and return.

Uses ``init_redis()`` (idempotent) rather than ``get_redis()`` because the ARQ
worker process does not run the API lifespan that initializes the client.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from app.config.redis import init_redis

log = logging.getLogger(__name__)

# query_brain / search read-cache TTL (PRD Feature 13: 5-min TTL, invalidated on
# any publish — which ``invalidate_skills`` does by clearing this whole keyspace).
SEARCH_TTL_SECONDS = 300


def search_key(workspace_id: str, query: str, kind: str = "query") -> str:
    """Cache key for a query result, on the invalidated ``skills:{ws}:*`` keyspace.

    ``kind`` discriminates callers that cache structurally different payloads under
    the same ``(workspace_id, query)`` — e.g. the skills surface (``"query"``) and
    brain-chat (``"brain"``) return incompatible contracts, so they must not share a
    key. All variants stay under ``skills:{ws}:*`` so ``invalidate_skills`` clears
    every facet on publish.
    """
    digest = hashlib.sha256(query.strip().lower().encode()).hexdigest()
    return f"skills:{workspace_id}:{kind}:{digest}"


async def get_cached_search(
    workspace_id: str, query: str, kind: str = "query"
) -> dict | None:
    """Return a cached query result, or ``None`` on miss/outage (best-effort)."""
    try:
        redis = await init_redis()
        raw = await redis.get(search_key(workspace_id, query, kind))
        return json.loads(raw) if raw else None
    except Exception:  # noqa: BLE001 — a cache outage must never fail a query
        log.warning("cache: search read failed for %s", workspace_id, exc_info=True)
        return None


async def set_cached_search(
    workspace_id: str, query: str, value: dict[str, Any], kind: str = "query"
) -> None:
    """Cache a query result for ``SEARCH_TTL_SECONDS`` (best-effort)."""
    try:
        redis = await init_redis()
        await redis.set(
            search_key(workspace_id, query, kind),
            json.dumps(value),
            ex=SEARCH_TTL_SECONDS,
        )
    except Exception:  # noqa: BLE001 — best-effort by design
        log.warning("cache: search write failed for %s", workspace_id, exc_info=True)


async def invalidate_skills(workspace_id: str) -> None:
    """Delete every cached skill entry for ``workspace_id`` (best-effort)."""
    try:
        redis = await init_redis()
        cursor = 0
        while True:
            cursor, keys = await redis.scan(
                cursor=cursor, match=f"skills:{workspace_id}:*", count=100
            )
            if keys:
                await redis.delete(*keys)
            if cursor == 0:
                return
    except Exception:  # noqa: BLE001 — invalidation is best-effort by design
        log.warning("cache: skill invalidation failed for %s", workspace_id, exc_info=True)
