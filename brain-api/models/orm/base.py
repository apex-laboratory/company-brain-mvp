from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy import MetaData, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from config import settings

engine = create_async_engine(
    settings.database_url,  # must be postgresql+asyncpg://...
    pool_size=5,
    max_overflow=10,
    echo=False,
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


# SQLAlchemy's built-in convention for unnamed indexes is "ix_%(column_0_label)s"
# — it uses only the FIRST column, so two unnamed indexes on the same table that
# share a first column collide (e.g. two ix_skills_workspace_id). Including every
# column name makes auto-generated index names unique. Alembic picks this up via
# target_metadata, so `op.create_index(None, ...)` in migrations names correctly.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Service-role session — bypasses RLS. Use only for admin/background ops."""
    async with AsyncSessionLocal() as session:
        yield session


@asynccontextmanager
async def run_in_tenant(
    workspace_id: str,
    user_id: str,
    role: str,
) -> AsyncGenerator[AsyncSession, None]:
    """
    Async context manager that opens a session and sets transaction-local GUCs
    so RLS policies (current_workspace_id(), current_member_role()) see the
    correct tenant context.

    The GUCs are transaction-local (set_config(..., true)) — they reset when
    the transaction ends and cannot leak across pooled connections.

    Usage:
        async with run_in_tenant(workspace_id, user_id, role) as db:
            result = await db.execute(select(Skill).where(...))
    """
    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(
                text(
                    "SELECT "
                    "set_config('app.current_workspace_id', :wid, true), "
                    "set_config('app.current_user_id',      :uid, true), "
                    "set_config('app.current_role',         :role, true)"
                ),
                {"wid": workspace_id, "uid": user_id, "role": role},
            )
            yield session
