"""ARQ enqueue side (BACKEND_BEST_PRACTICES.md §13).

The API process enqueues jobs through a lazily-created, cached ``ArqRedis`` pool.
Enqueuing is best-effort from request handlers: a queue outage must not fail the
user-facing request (the OAuth callback still succeeds; the sweep is retried).
"""
from __future__ import annotations

import logging

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from app.config.settings import settings

log = logging.getLogger(__name__)

_pool: ArqRedis | None = None


async def get_queue() -> ArqRedis:
    """Return the shared ARQ pool, creating it on first use."""
    global _pool
    if _pool is None:
        _pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    return _pool


async def enqueue(function: str, *args: object, **kwargs: object) -> None:
    """Enqueue an ARQ job by function name. Logs and swallows queue errors."""
    try:
        pool = await get_queue()
        await pool.enqueue_job(function, *args, **kwargs)
    except Exception:  # noqa: BLE001 — enqueue must never break the caller
        log.exception("Failed to enqueue job %s", function)


async def close_queue() -> None:
    """Close the ARQ pool on shutdown."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
