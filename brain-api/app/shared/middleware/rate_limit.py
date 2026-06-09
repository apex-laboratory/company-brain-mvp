"""Rate limiting (BACKEND_BEST_PRACTICES.md §9, API_DOCUMENTATION.md §Rate Limits).

slowapi with a Redis backend so limits hold across instances. Per-surface key
functions: IP for auth, user_id for dashboard/OAuth callbacks, workspace_id for
brain/skills. Limit values are applied per-route via ``@limiter.limit(...)``.
"""
from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request

from app.config.redis import get_redis_url

# Per-surface defaults (documented; applied at the route).
AUTH_LIMIT = "10/minute"
OAUTH_CALLBACK_LIMIT = "20/minute"
DASHBOARD_LIMIT = "300/minute"

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
    """Key by workspace, falling back to IP before auth resolves."""
    auth = getattr(request.state, "auth", None)
    if auth is not None:
        return f"workspace:{auth.workspace_id}"
    return get_remote_address(request)
