from fastapi import APIRouter
from database import get_pool
from cache import get_redis

router = APIRouter()


@router.get("/health")
async def health():
    status = {"status": "ok", "db": False, "redis": False}
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        status["db"] = True
    except Exception:
        pass
    try:
        r = await get_redis()
        await r.ping()
        status["redis"] = True
    except Exception:
        pass
    return status
