"""Rate limiting (BACKEND_BEST_PRACTICES.md §9, API_DOCUMENTATION.md §Rate Limits).

slowapi with a Redis backend so limits hold across instances. Per-surface key
functions: IP for auth, user_id for dashboard/OAuth callbacks, workspace_id for
brain/skills. Limit values are applied per-route via ``@limiter.limit(...)``.
"""
from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request

from app.config.redis import get_redis, get_redis_url
from app.shared.errors.app_error import RateLimitError

# Per-surface defaults (documented; applied at the route).
AUTH_LIMIT = "10/minute"
OAUTH_CALLBACK_LIMIT = "20/minute"
DASHBOARD_LIMIT = "300/minute"

# AUTH_LIMIT expressed as raw numbers for the manual API-key check below.
_API_KEY_LIMIT = 10
_API_KEY_WINDOW_SECONDS = 60

limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=get_redis_url(),
    default_limits=[DASHBOARD_LIMIT],
)


def user_key(request: Request) -> str:
    """Key by authenticated user, falling back to IP before auth resolves."""
    auth = getattr(request.state, "auth", None)
    if auth is not None:
        return f"user:{auth.user_id}"
    return get_remote_address(request)


def workspace_key(request: Request) -> str:
    """Key by workspace, falling back to IP before auth resolves or when the
    caller has no workspace yet (pre-onboarding tokens have workspace_id=None)."""
    auth = getattr(request.state, "auth", None)
    if auth is not None and auth.workspace_id is not None:
        return f"workspace:{auth.workspace_id}"
    return get_remote_address(request)


async def enforce_api_key_rate_limit(request: Request) -> None:
    """Apply AUTH_LIMIT to the per-IP API-key path before the credential lookup.

    The slowapi ``@limiter.limit`` decorators are route-scoped, so the API-key
    branch of the auth dependency would otherwise inherit only the 300/minute
    DASHBOARD_LIMIT — enough to brute-force key hashes. This caps unauthenticated
    key probes at AUTH_LIMIT (10/minute) per IP using a Redis fixed-window
    counter shared across instances. Raises ``RateLimitError`` (429) when the
    window is exhausted, *before* any DB hit.
    """
    ip = get_remote_address(request)
    redis = get_redis()
    key = f"ratelimit:apikey:{ip}"
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, _API_KEY_WINDOW_SECONDS)
    if count > _API_KEY_LIMIT:
        ttl = await redis.ttl(key)
        raise RateLimitError(retry_after=ttl if ttl > 0 else _API_KEY_WINDOW_SECONDS)
