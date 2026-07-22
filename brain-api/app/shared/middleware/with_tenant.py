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

from app.config.database import engine as _privileged_engine
from app.config.database import get_tenant_session
from app.config.database import tenant_engine as _tenant_engine

if TYPE_CHECKING:
    from app.shared.middleware.authenticate import AuthContext


def _assert_tenant_bound(session: AsyncSession) -> None:
    """Fail closed if a tenant block is handed a session on the PRIVILEGED pool.

    RLS only isolates when the connection's role is subject to it; the privileged
    role (BYPASSRLS) sets the GUCs but ignores the policies, silently leaking
    other tenants' rows. We only enforce when the two pools are genuinely distinct
    (``TENANT_DATABASE_URL`` set) — under the dev fallback they share one engine
    and there is nothing to distinguish. A mocked session in unit tests has no real
    bind, so this check is a no-op there.
    """
    if _privileged_engine is _tenant_engine:
        return
    # AsyncSession.bind is the AsyncEngine the sessionmaker was bound to
    # (get_bind() returns the underlying *sync* engine, which never matches).
    bind = getattr(session, "bind", None)
    if bind is _privileged_engine:
        raise RuntimeError(
            "run_in_tenant received a session from the PRIVILEGED pool (get_session). "
            "Open tenant-scoped work with get_tenant_session so Postgres RLS isolates "
            "the workspace — otherwise the query bypasses RLS and leaks other tenants."
        )


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

    IMPORTANT: ``session`` MUST come from the RESTRICTED pool
    (``get_tenant_session``), never the privileged ``get_session``. The GUCs only
    enforce isolation when the connecting role is subject to RLS; on the
    BYPASSRLS role they are set but ignored, silently leaking other tenants' rows.
    """
    _assert_tenant_bound(session)
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

    Bundles the ``get_tenant_session`` + ``run_in_tenant`` preamble that every
    workspace-scoped service uses. Opens on the RESTRICTED (RLS-subject) pool so
    isolation is enforced by Postgres, not just by convention. The caller still
    owns the transaction and commits its own writes; the GUCs are
    transaction-local. ``role`` falls back to ``viewer`` for contexts that carry
    no role (least privilege for RLS).
    """
    async with get_tenant_session() as session, run_in_tenant(
        session, workspace_id, auth.user_id, auth.role or "viewer"
    ):
        yield session
