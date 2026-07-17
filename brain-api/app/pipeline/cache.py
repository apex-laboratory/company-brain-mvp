"""Skill-cache invalidation on publish (spec: ``services/skill_writer.py``).

The read-side cache (`skills:{workspace_id}:*`) is populated by Phase 5's
``query_brain``; invalidating the key space now keeps every publish/approve
path forward-compatible. Best-effort: a Redis outage must not fail a skill
write (worst case is a stale cache entry until TTL), so errors log and return.

Uses ``init_redis()`` (idempotent) rather than ``get_redis()`` because the ARQ
worker process does not run the API lifespan that initializes the client.
"""
from __future__ import annotations

import logging

from app.config.redis import init_redis

log = logging.getLogger(__name__)


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
