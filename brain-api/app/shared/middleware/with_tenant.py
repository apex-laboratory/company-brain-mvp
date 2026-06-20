"""Tenant context for RLS (BACKEND_BEST_PRACTICES.md §8, DB_DESIGN §RLS).

Because we issue our own JWTs (not Supabase), the backend sets the RLS session
variables itself. ``run_in_tenant`` sets the three ``app.current_*`` GUCs as
**transaction-local** (``set_config(..., true)``) so they reset at transaction
end and cannot bleed across pooled connections. RLS policies read them via
``current_workspace_id()`` / ``current_member_role()``.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_session

if TYPE_CHECKING:
    from app.shared.middleware.authenticate import AuthContext


@asynccontextmanager
async def run_in_tenant(
    session: AsyncSession,
    workspace_id: str,
    user_id: str,
    role: str,
) -> AsyncGenerator[AsyncSession, None]:
    """Set transaction-local tenant GUCs for the duration of the block.

    The caller owns the transaction; the GUCs are scoped to it. A single
    round-trip sets all three settings.
    """
    await session.execute(
        text(
            "SELECT "
            "set_config('app.current_workspace_id', :workspace_id, true), "
            "set_config('app.current_user_id', :user_id, true), "
            "set_config('app.current_role', :role, true)"
        ),
        {"workspace_id": workspace_id, "user_id": user_id, "role": role},
    )
    yield session


@asynccontextmanager
async def tenant_session(
    auth: AuthContext, workspace_id: str
) -> AsyncGenerator[AsyncSession, None]:
    """Open a DB session already scoped to ``auth``'s tenant context.

    Bundles the ``get_session`` + ``run_in_tenant`` preamble that every
    workspace-scoped service uses. The caller still owns the transaction and
    commits its own writes; the GUCs are transaction-local. ``role`` falls back
    to ``viewer`` for contexts that carry no role (least privilege for RLS).
    """
    async with get_session() as session, run_in_tenant(
        session, workspace_id, auth.user_id, auth.role or "viewer"
    ):
        yield session
