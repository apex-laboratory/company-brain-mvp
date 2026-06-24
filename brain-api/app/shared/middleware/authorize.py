"""Role / scope authorization dependencies (BACKEND_BEST_PRACTICES.md §7).

``require_role`` enforces the member-role hierarchy for dashboard (JWT) routes;
``require_scope`` enforces scopes for API-key routes. Both run after
``get_auth_context`` and raise ``ForbiddenError`` (403) when unsatisfied.
Authorization is also backstopped by RLS in the DB (defense in depth).
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Depends

from app.shared.errors.app_error import ForbiddenError
from app.shared.middleware.authenticate import AuthContext, get_auth_context

# Higher rank satisfies any lower-or-equal role requirement.
_ROLE_RANK: dict[str, int] = {"viewer": 1, "editor": 2, "admin": 3}


def assert_workspace_member(auth: AuthContext, workspace_id: str) -> str:
    """Return the caller's workspace, or 403 if it isn't the one in the path.

    The JWT/API-key context carries the single workspace the caller belongs to;
    a request for any other ``workspace_id`` is not theirs to act on. RLS is also
    driven from the auth context (never the path), so a mismatch could not be
    served anyway — failing closed here makes that explicit. Services call this
    before opening a tenant-scoped transaction.
    """
    if auth.workspace_id is None or workspace_id != auth.workspace_id:
        raise ForbiddenError("You are not a member of this workspace.")
    return auth.workspace_id


def require_role(minimum: str) -> Callable[[AuthContext], Awaitable[None]]:
    """Require the caller's role to be ``minimum`` or higher in the hierarchy."""
    required_rank = _ROLE_RANK[minimum]

    async def dependency(auth: AuthContext = Depends(get_auth_context)) -> None:
        if _ROLE_RANK.get(auth.role or "", 0) < required_rank:
            raise ForbiddenError()

    return dependency


def require_scope(*scopes: str) -> Callable[[AuthContext], Awaitable[None]]:
    """Require the caller (typically an API key) to hold all of ``scopes``."""

    async def dependency(auth: AuthContext = Depends(get_auth_context)) -> None:
        held = set(auth.scopes)
        if not held.issuperset(scopes):
            raise ForbiddenError()

    return dependency
