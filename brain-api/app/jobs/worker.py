"""ARQ worker definition (BACKEND_BEST_PRACTICES.md §13).

Runs in a separate process from the API: ``arq app.jobs.worker.WorkerSettings``.
Each job is idempotent (dedupe on a stable external id) and retried with backoff.
"""
from __future__ import annotations

from arq import cron
from arq.connections import RedisSettings

from app.config.settings import settings
from app.integrations.base import close_http_client
from app.jobs.tasks.google_watch import watch_register, watch_renew
from app.jobs.tasks.source_sync import source_sync
from app.jobs.tasks.webhook_ingest import webhook_ingest


async def shutdown(ctx: dict) -> None:
    """Close shared resources when the worker stops."""
    await close_http_client()


class WorkerSettings:
    functions = [source_sync, webhook_ingest, watch_register]
    # Daily renewal of Google push channels (Drive/Gmail watch expires <= 7 days).
    cron_jobs = [cron(watch_renew, hour=3, minute=0)]
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    on_shutdown = shutdown
    max_jobs = 10
    job_timeout = 600
    retry_jobs = True
    max_tries = 3
