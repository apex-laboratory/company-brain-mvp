"""FastAPI application factory (BACKEND_BEST_PRACTICES.md §6, §16).

Wires the middleware chain, exception handlers, rate limiter, lifespan, and
router mounting. No business logic lives here.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any, cast

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import text

from app.config.database import close_db_pool, get_session
from app.config.redis import close_redis, get_redis, init_redis
from app.config.settings import settings
from app.integrations.base import close_http_client
from app.jobs.queue import close_queue
from app.modules.api_keys.router import router as api_keys_router
from app.modules.auth.router import router as auth_router
from app.modules.dashboard.router import router as dashboard_router
from app.modules.members.router import router as members_router
from app.modules.sources.router import router as sources_router
from app.modules.sweeps.router import router as sweeps_router
from app.modules.webhooks.router import router as webhooks_router
from app.modules.workspaces.router import router as workspaces_router
from app.shared.middleware.error_handler import register_exception_handlers
from app.shared.middleware.rate_limit import limiter
from app.shared.middleware.request_context import RequestContextMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    await init_redis()
    yield
    await close_redis()
    await close_queue()
    await close_http_client()
    await close_db_pool()


_is_prod = settings.environment == "production"

app = FastAPI(
    title="Brainite API",
    version="1.0.0",
    docs_url=None if _is_prod else "/docs",
    redoc_url=None if _is_prod else "/redoc",
    lifespan=lifespan,
)

# Middleware chain (outermost first).
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,  # required for the refresh-token cookie
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
app.add_middleware(RequestContextMiddleware)

app.state.limiter = limiter
# slowapi's handler signature (Request, RateLimitExceeded) is narrower than
# Starlette's (Request, Exception); the cast bridges that known mismatch.
app.add_exception_handler(RateLimitExceeded, cast(Any, _rate_limit_exceeded_handler))
register_exception_handlers(app)

app.include_router(auth_router, prefix="/api/v1")
app.include_router(sources_router, prefix="/api/v1")
app.include_router(webhooks_router, prefix="/api/v1")
app.include_router(dashboard_router, prefix="/api/v1")
app.include_router(workspaces_router, prefix="/api/v1")
app.include_router(api_keys_router, prefix="/api/v1")
app.include_router(members_router, prefix="/api/v1")
app.include_router(sweeps_router, prefix="/api/v1")


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe."""
    return {"status": "ok"}


@app.get("/ready")
async def ready() -> dict[str, bool]:
    """Readiness probe: DB + Redis reachable."""
    async with get_session() as session:
        await session.execute(text("SELECT 1"))
    await get_redis().ping()
    return {"database": True, "redis": True}
