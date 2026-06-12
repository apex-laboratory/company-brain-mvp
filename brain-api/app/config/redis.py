"""Redis connection pool.

Shared store for rate limiting (slowapi) and the ARQ job queue, so limits and
queues hold across instances.
"""
from __future__ import annotations

from redis.asyncio import Redis

from app.config.settings import settings

_client: Redis | None = None


def get_redis_url() -> str:
    """Return the configured Redis DSN (used by the slowapi limiter factory)."""
    return settings.redis_url


async def init_redis() -> Redis:
    """Create the shared Redis client (called from the app lifespan)."""
    global _client
    if _client is None:
        _client = Redis.from_url(settings.redis_url, decode_responses=True)
    return _client


def get_redis() -> Redis:
    """Return the initialized Redis client; raises if the lifespan never ran."""
    if _client is None:
        raise RuntimeError("Redis client not initialized")
    return _client


async def close_redis() -> None:
    """Close the Redis client (graceful shutdown)."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
