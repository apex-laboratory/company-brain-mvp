"""Rate limiting (BACKEND_BEST_PRACTICES.md §9, API_DOCUMENTATION.md §Rate Limits).

Async Redis fixed-window counters so limits hold across instances. Per-surface
key functions: IP for auth, user_id for dashboard/OAuth callbacks, workspace_id
for brain/skills. Limit values are applied per-route via ``@limiter.limit(...)``.

``limiter`` used to be a slowapi ``Limiter``; slowapi only supports synchronous
storage, so every decorated route blocked the event loop on Redis I/O. The
drop-in ``_AsyncRouteLimiter`` keeps the decorator surface (and the
``limiter.enabled`` toggle the tests use) but awaits the same fixed-window
counter as the manual checks below.
"""
from __future__ import annotations

import functools
import inspect
from collections.abc import Callable

from starlette.requests import Request

from app.config.redis import get_redis
from app.shared.errors.app_error import RateLimitError


def get_remote_address(request: Request) -> str:
    """Client IP key (the pre-auth fallback for every surface)."""
    return request.client.host if request.client else "127.0.0.1"

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
# Agent-run ingestion. Higher than BRAIN_LIMIT because the caller is an editor
# hook, not a human: a busy developer closes many short runs per minute and a
# throttled push is a *lost* run — the client discards its spool entry on any
# terminal response. Keyed by workspace, like the rest of the agent surface.
RUNS_INGEST_LIMIT = "300/minute"

# AUTH_LIMIT / BRAIN_LIMIT expressed as raw numbers for the manual checks below
# (used off the slowapi decorator path: the API-key auth branch and the MCP
# transport, which has no route to decorate).
_API_KEY_LIMIT = 10
_API_KEY_WINDOW_SECONDS = 60
_BRAIN_LIMIT = 120
_BRAIN_WINDOW_SECONDS = 60

_UNIT_SECONDS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}


def _parse_limit(value: str) -> tuple[int, int]:
    """``"120/minute"`` → ``(120, 60)``."""
    amount, _, unit = value.partition("/")
    return int(amount), _UNIT_SECONDS[unit.strip()]


class _AsyncRouteLimiter:
    """slowapi-shaped ``@limiter.limit(...)`` decorator over the async fixed window.

    Counters are keyed per endpoint function + caller subject, so two routes
    sharing a limit value don't share a budget (same behavior as slowapi's
    per-route keys). Raises the typed ``RateLimitError`` (429 envelope) instead
    of slowapi's ``RateLimitExceeded``.
    """

    def __init__(self, key_func: Callable[[Request], str]) -> None:
        self.key_func = key_func
        self.enabled = True  # unit tests flip this off to skip Redis

    def limit(
        self, limit_value: str, key_func: Callable[[Request], str] | None = None
    ) -> Callable:
        amount, window = _parse_limit(limit_value)
        resolve_key = key_func or self.key_func

        def decorator(func: Callable) -> Callable:
            bucket = f"route:{func.__module__}.{func.__qualname__}"

            @functools.wraps(func)
            async def wrapper(*args, **kwargs):
                if self.enabled:
                    request = kwargs.get("request")
                    if not isinstance(request, Request):
                        request = next(
                            (a for a in args if isinstance(a, Request)), None
                        )
                    if request is None:
                        raise RuntimeError(
                            "BUG: @limiter.limit endpoint "
                            f"{func.__qualname__} has no 'request' parameter"
                        )
                    await _enforce_fixed_window(
                        bucket, resolve_key(request), amount, window
                    )
                result = func(*args, **kwargs)
                if inspect.isawaitable(result):
                    result = await result
                return result

            return wrapper

        return decorator


limiter = _AsyncRouteLimiter(key_func=get_remote_address)


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
