"""ARQ worker definition (BACKEND_BEST_PRACTICES.md §13).

Runs in a separate process from the API: ``arq app.jobs.worker.WorkerSettings``.
Each job is idempotent (dedupe on a stable external id) and retried with backoff.
"""
from __future__ import annotations

from arq import cron
from arq.connections import RedisSettings
from arq.worker import func

from app.config.settings import settings
from app.integrations.base import close_http_client
from app.jobs.tasks.brain_index_backfill import brain_index_backfill
from app.jobs.tasks.extract_event import extract_event
from app.jobs.tasks.gate_run import gate_run
from app.jobs.tasks.google_watch import watch_register, watch_renew
from app.jobs.tasks.onboarding_sweep import onboarding_sweep
from app.jobs.tasks.poll_sync import poll_pull_sources
from app.jobs.tasks.query_extract import query_extract
from app.jobs.tasks.reconcile_events import reenqueue_stale_events
from app.jobs.tasks.reconcile_push_sync import reconcile_push_sources
from app.jobs.tasks.reembed_skills import reembed_skills
from app.jobs.tasks.source_sync import source_sync
from app.jobs.tasks.sweep_extract import sweep_extract
from app.jobs.tasks.sync_agent_session import sync_agent_session
from app.jobs.tasks.webhook_ingest import webhook_ingest


async def startup(ctx: dict) -> None:
    """Fail loudly at boot if a configured LLM model has no price entry, so a
    model rotation can't silently zero the pipeline's cost telemetry.

    Only the model behind the active ``LLM_PROVIDER`` is checked — switching to
    OpenRouter for a demo shouldn't require Gemini/Anthropic to stay priced."""
    from app.pipeline.llm.pricing import ensure_priced

    active_model = {
        "gemini": settings.gemini_model,
        "anthropic": settings.anthropic_model,
        "openrouter": settings.openrouter_model,
    }[settings.llm_provider]
    ensure_priced(active_model, settings.embedding_model)


async def shutdown(ctx: dict) -> None:
    """Close shared resources when the worker stops."""
    await close_http_client()


class WorkerSettings:
    # The sweep backfills every source's history sequentially, so it gets an hour
    # instead of the default 10-minute job_timeout; a timeout kill is resumed by
    # the ARQ retry (completed sources are skipped).
    # extract_event dead-letters internally (outcome='failed') and never re-raises,
    # so ARQ's retry_jobs won't stack on top of the pipeline's own LLM retries. Its
    # own retry budget (llm_max_attempts, each waiting up to _MAX_RETRY_AFTER on a
    # 429) can now exceed the default 10-minute job_timeout, so it gets 30 minutes
    # like the other LLM-heavy jobs — a timeout kill here WOULD stack an ARQ retry
    # on top of an in-flight pipeline retry, so the wider budget matters.
    functions = [
        source_sync,
        webhook_ingest,
        # One vendor round-trip and one small UPDATE. The default timeout is
        # already generous; what matters is that it is off the webhook's request
        # path, not that it gets a budget of its own.
        sync_agent_session,
        watch_register,
        func(extract_event, timeout=1800),
        func(onboarding_sweep, timeout=3600),
        # A large historical backfill can take a while to extract; give it an hour
        # like the sweep itself. A timeout kill resumes on retry (queued events only).
        func(sweep_extract, timeout=3600),
        query_extract,
        # Deterministic predicates + one vector lookup — no LLM, so the default
        # 10-minute timeout is generous. It runs on every ingested run, which is
        # exactly why it must stay cheap (PRD Feature 30).
        gate_run,
        # Embedding every skill version can take a while on a large workspace; give it
        # an hour. Idempotent (skips already-indexed chunks), so a retry resumes cheaply.
        func(brain_index_backfill, timeout=3600),
        # Re-embedding the skills table after a model change is the same shape of
        # work as the brain backfill: hour-long budget, resumable (it re-selects only
        # rows still on the old model), so a timeout kill costs nothing already spent.
        func(reembed_skills, timeout=3600),
    ]
    # Daily renewal of Google push channels (Drive/Gmail watch expires <= 7 days);
    # 15-minute polling for providers without push delivery (Notion).
    cron_jobs = [
        cron(watch_renew, hour=3, minute=0),
        cron(poll_pull_sources, minute={0, 15, 30, 45}),
        # Backstop: re-enqueue events stranded at outcome='queued' (swallowed
        # extract enqueue, chained-backfill chunks, sweep_extract timeout).
        cron(reenqueue_stale_events, minute={5, 20, 35, 50}),
        # Backstop for push providers (GitHub, Slack): last_synced_at only
        # advances on a successful sync, so a dropped webhook looks identical
        # to a quiet source — push is still the primary path, this just catches
        # misses. Cadence must stay in step with _BUCKET_SECONDS in
        # reconcile_push_sync (its dedupe bucket), or arq collapses every tick
        # inside one bucket into a single enqueue.
        cron(reconcile_push_sources, minute=set(range(0, 60, 5))),
    ]
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    on_startup = startup
    on_shutdown = shutdown
    max_jobs = 10
    job_timeout = 600
    retry_jobs = True
    max_tries = 3
