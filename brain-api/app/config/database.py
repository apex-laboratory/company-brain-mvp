"""SQLAlchemy 2.0 async engine + session factory.

The default session is a *service-role* session (no tenant context). Workspace
traffic must wrap queries in ``run_in_tenant`` (shared/middleware/with_tenant.py)
so Postgres RLS sees the caller's workspace/role.
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

engine: AsyncEngine = create_async_engine(
    settings.database_url,  # must be postgresql+asyncpg://...
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,
    echo=False,
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Open a service-role session (bypasses tenant scoping).

    Use for pre-tenant lookups (resolve API key / user by email) and admin work.
    For workspace-scoped queries, wrap with ``run_in_tenant``.
    """
    async with AsyncSessionLocal() as session:
        yield session


async def close_db_pool() -> None:
    """Dispose the engine and its connection pool (graceful shutdown)."""
    await engine.dispose()
