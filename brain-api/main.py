import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
from routers import health, skills, review
from database import init_db_pool, close_db_pool
from cache import init_redis, close_redis
from mcp_server.server import run_mcp_server
from config import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db_pool()
    await init_redis()
    asyncio.create_task(run_mcp_server())
    yield
    await close_db_pool()
    await close_redis()


app = FastAPI(title="Company Brain API", version="1.0.0", lifespan=lifespan)

app.include_router(health.router)
app.include_router(skills.router, prefix="/skills")
app.include_router(review.router, prefix="/review")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=settings.api_port, reload=False)
