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
# API-key creation is expensive and security-sensitive (each mints a live
# credential); cap it per admin (API_DOCUMENTATION.md §Rate Limits).
API_KEY_CREATE_LIMIT = "5/hour"
# Agent-facing brain surface (search, skill reads, override feedback). Keyed by
# workspace so one workspace's agents can't exhaust another's budget.
BRAIN_LIMIT = "120/minute"
# Full-corpus export is heavy and dumps all organizational knowledge; cap hard.
EXPORT_LIMIT = "10/hour"

# AUTH_LIMIT / BRAIN_LIMIT expressed as raw numbers for the manual checks below
# (used off the slowapi decorator path: the API-key auth branch and the MCP
# transport, which has no route to decorate).
_API_KEY_LIMIT = 10
_API_KEY_WINDOW_SECONDS = 60
_BRAIN_LIMIT = 120
_BRAIN_WINDOW_SECONDS = 60

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


async def _enforce_fixed_window(bucket: str, subject: str, limit: int, window: int) -> None:
    """Increment a Redis fixed-window counter, raising 429 past ``limit``.

    Shared across instances (the counter lives in Redis) and evaluated *before*
    any DB hit, so an exhausted window costs no query. Used off the slowapi
    decorator path — see the two callers below.
    """
    redis = get_redis()
    key = f"ratelimit:{bucket}:{subject}"
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, window)
    if count > limit:
        ttl = await redis.ttl(key)
        raise RateLimitError(retry_after=ttl if ttl > 0 else window)


async def enforce_api_key_probe_limit(ip: str) -> None:
    """Cap unauthenticated ``X-API-Key`` probes at AUTH_LIMIT (10/minute) per IP.

    Any transport that resolves a raw key outside the slowapi decorator path
    must call this *before* the credential lookup, or key hashes can be
    brute-forced. Two callers: the REST auth dependency (via
    ``enforce_api_key_rate_limit``) and the MCP ``query_brain`` tool, whose SSE
    process has no route to decorate.
    """
    await _enforce_fixed_window("apikey", ip, _API_KEY_LIMIT, _API_KEY_WINDOW_SECONDS)


async def enforce_brain_query_limit(workspace_id: str) -> None:
    """Apply BRAIN_LIMIT (120/minute) per workspace to an authenticated brain read.

    The manual equivalent of ``@limiter.limit(BRAIN_LIMIT, key_func=workspace_key)``
    on the REST brain routes, for the MCP transport. Keyed by workspace so one
    workspace's agents can't exhaust another's budget.
    """
    await _enforce_fixed_window("brain", workspace_id, _BRAIN_LIMIT, _BRAIN_WINDOW_SECONDS)


async def enforce_api_key_rate_limit(request: Request) -> None:
    """Apply AUTH_LIMIT to the per-IP API-key path before the credential lookup.

    The slowapi ``@limiter.limit`` decorators are route-scoped, so the API-key
    branch of the auth dependency would otherwise inherit only the 300/minute
    DASHBOARD_LIMIT — enough to brute-force key hashes. Raises
    ``RateLimitError`` (429) when the window is exhausted, *before* any DB hit.
    """
    await enforce_api_key_probe_limit(get_remote_address(request))
