"""SQLAlchemy 2.0 async engines + session factories (two-pool tenancy).

Two roles, two pools, one hard rule about which to use:

* **Privileged pool** (``get_session``) — connects as the ``DATABASE_URL`` role
  (Supabase ``postgres``, which has ``BYPASSRLS``). Use ONLY for work that has no
  workspace context yet and legitimately spans tenants: auth (users, refresh
  tokens), ``oauth_states`` resolution, and cross-workspace admin/cron lookups.

* **Restricted pool** (``get_tenant_session``) — connects as the RLS-subject
  ``brain_app`` role (``TENANT_DATABASE_URL``, no ``BYPASSRLS``). Use for EVERY
  workspace-scoped query, always wrapped in ``run_in_tenant`` so the tenant GUCs
  are set. Because this role cannot bypass RLS, a missing/incorrect workspace GUC
  fails closed (zero rows) instead of leaking another tenant's data.

If ``TENANT_DATABASE_URL`` is unset the restricted pool falls back to the
privileged URL with a loud warning — RLS then provides no isolation, so it must
be configured in any shared deployment.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config.settings import settings
from app.shared.logger import get_logger

log = get_logger()

# ── privileged pool (BYPASSRLS role: auth / oauth_states / cross-tenant) ────────
engine: AsyncEngine = create_async_engine(
    settings.database_url,  # must be postgresql+asyncpg://...
    pool_size=5,
    max_overflow=10,
    # pre_ping costs ~3 WAN round-trips per checkout against the Supabase pooler;
    # recycling idle connections after 5 min catches stale ones for free instead.
    pool_recycle=300,
    echo=False,
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

# ── restricted pool (RLS-subject role: all tenant-scoped traffic) ───────────────
_tenant_url = settings.tenant_database_url or settings.database_url
if not settings.tenant_database_url:
    log.warning(
        "tenant_database_url_unset",
        detail=(
            "TENANT_DATABASE_URL is not set; tenant traffic falls back to the "
            "privileged DATABASE_URL role. RLS will NOT isolate tenants. Run "
            "scripts/provision_tenant_role.py and set TENANT_DATABASE_URL."
        ),
    )

tenant_engine: AsyncEngine = create_async_engine(
    _tenant_url,
    pool_size=5,
    max_overflow=10,
    pool_recycle=300,  # same rationale as the privileged pool above
    echo=False,
)

AsyncTenantSessionLocal = async_sessionmaker(
    tenant_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Open a PRIVILEGED session (BYPASSRLS role, no tenant scoping).

    Use only for pre-tenant lookups (resolve API key / user by email, oauth_states)
    and genuinely cross-workspace admin work. For any workspace-scoped query use
    ``get_tenant_session`` + ``run_in_tenant`` (or ``tenant_session``).
    """
    async with AsyncSessionLocal() as session:
        yield session


@asynccontextmanager
async def get_tenant_session() -> AsyncGenerator[AsyncSession, None]:
    """Open a RESTRICTED session (RLS-subject role) for tenant-scoped queries.

    MUST be wrapped in ``run_in_tenant`` (or opened via ``tenant_session``) so the
    ``app.current_*`` GUCs are set; without them RLS returns zero rows. This is the
    only correct opener for any query that reads or writes workspace data.
    """
    async with AsyncTenantSessionLocal() as session:
        yield session


async def close_db_pool() -> None:
    """Dispose both engines and their connection pools (graceful shutdown)."""
    await engine.dispose()
    await tenant_engine.dispose()
