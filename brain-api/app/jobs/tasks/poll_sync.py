"""``poll_pull_sources`` cron — periodic sync for providers without push (KAN-2).

Most providers push changes to us (Slack Events API, GitHub App webhooks, Google
watch channels). Providers that can't — Notion has no webhooks at all — would
otherwise sync once during the onboarding sweep and then go silent forever. This
cron keeps them current ("living currency", PRD §6): every 15 minutes it enqueues
one ``source_sync`` per connected pull-only connection.

Which providers poll is derived from the integration registry (an integration
declares ``push_delivery = False``), so Jira/Zendesk opt in by setting the flag —
no cron change needed. Jobs are enqueued with a stable ``_job_id`` so a slow sync
is never stacked behind a duplicate of itself.
"""
from __future__ import annotations

import logging
import time

from app.config.database import get_session
from app.integrations import REGISTRY
from app.jobs.queue import enqueue
from app.jobs.repository import JobsRepository

log = logging.getLogger(__name__)

_repo = JobsRepository()


def _pull_providers() -> list[str]:
    return [
        provider
        for provider, integration in REGISTRY.items()
        if not getattr(integration, "push_delivery", True)
    ]


async def poll_pull_sources(ctx: dict) -> dict:
    """ARQ cron entrypoint. ``ctx`` is the ARQ job context (unused)."""
    providers = _pull_providers()
    if not providers:
        return {"enqueued": 0}

    async with get_session() as session:
        connections = await _repo.list_pollable_connections(session, providers)

    # The job id dedupes within one 15-minute bucket (a slow sync is never stacked
    # behind a duplicate of itself) without blocking the *next* tick — arq holds a
    # plain id until the previous result expires, which would throttle the cadence.
    bucket = int(time.time() // 900)
    for source_id, workspace_id in connections:
        await enqueue(
            "source_sync", workspace_id, source_id,
            _job_id=f"poll-sync:{source_id}:{bucket}",
        )

    if connections:
        log.info("poll_pull_sources: enqueued %d syncs (%s)", len(connections), providers)
    return {"enqueued": len(connections)}
