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

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


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
