"""``reconcile_push_sources`` cron — backstop for missed webhook deliveries.

Push providers (GitHub, Slack) rely entirely on webhooks for live updates.
Unlike ``poll_pull_sources``, nothing re-syncs them on a schedule — so if the
API was down when a webhook fired, or a provider's own redelivery window
lapsed, the update is gone with nothing to notice: a connection's cursor
(``last_synced_at``) only advances on a *successful* sync, so a dead webhook
path is indistinguishable from a quiet source.

This cron re-syncs every push-delivery connection hourly (vs. the pull cron's
15 minutes — push is still the primary path, this is only a safety net).
``source_sync`` re-fetches "changed since cursor" per connection, so a quiet
source costs one cheap API call and the cursor doesn't move.
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


def _push_providers() -> list[str]:
    return [
        provider
        for provider, integration in REGISTRY.items()
        if getattr(integration, "push_delivery", True)
    ]


async def reconcile_push_sources(ctx: dict) -> dict:
    """ARQ cron entrypoint. ``ctx`` is the ARQ job context (unused)."""
    providers = _push_providers()
    if not providers:
        return {"enqueued": 0}

    async with get_session() as session:
        connections = await _repo.list_pollable_connections(session, providers)

    # Hour bucket, mirroring poll_pull_sources: dedupes within the hour without
    # blocking the *next* tick — arq holds a plain id until the previous
    # result expires, which would throttle the cadence.
    bucket = int(time.time() // 3600)
    for source_id, workspace_id in connections:
        await enqueue(
            "source_sync", workspace_id, source_id,
            _job_id=f"reconcile-push:{source_id}:{bucket}",
        )

    if connections:
        log.info(
            "reconcile_push_sources: enqueued %d syncs (%s)", len(connections), providers
        )
    return {"enqueued": len(connections)}
