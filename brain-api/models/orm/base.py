import json
from typing import AsyncGenerator

from sqlalchemy import text
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


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Service-role session — bypasses RLS. Use only for admin/background ops."""
    async with AsyncSessionLocal() as session:
        yield session


async def get_tenant_db(claims: dict) -> AsyncGenerator[AsyncSession, None]:
    """
    Session with request.jwt.claims set for the current transaction.

    RLS policies call current_org_id() which reads this claim, so every query
    is automatically scoped to the caller's org. Use this for all user-facing
    endpoints — never get_db() for tenant data.

    Usage in FastAPI:
        async def my_route(token_claims: dict = Depends(get_claims)):
            async with get_tenant_db(token_claims) as db:
                ...
    """
    async with AsyncSessionLocal() as session:
        # transaction-local: resets when the transaction ends
        await session.execute(
            text("SELECT set_config('request.jwt.claims', :claims, true)"),
            {"claims": json.dumps(claims)},
        )
        yield session
