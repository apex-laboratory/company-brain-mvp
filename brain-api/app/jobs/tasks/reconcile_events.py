"""``reenqueue_stale_events`` cron — backstop for stranded ``queued`` events.

Ingestion commits a ``source_events`` row at ``outcome='queued'`` and then, as a
*separate* step, enqueues its extraction. That second step can silently never
happen: a Redis outage swallows the ``extract_event`` enqueue (``queue.py``), a
chained backfill chunk's events are deferred to ``sweep_extract`` which selects
its work set once, or a large ``sweep_extract`` times out mid-launch. Nothing
else re-scans those rows, so they sit ``queued`` forever — silent data loss.

This cron closes the gap: every few minutes it re-enqueues ``extract_event`` for
any event still ``queued`` past a grace window. It is safe to run repeatedly —
``run_pipeline`` skips already-``processed`` events, and the stable ``_job_id``
keeps a still-running re-extraction from stacking a duplicate.
"""
from __future__ import annotations

import logging

from app.config.database import get_session
from app.jobs.queue import enqueue
from app.jobs.repository import JobsRepository

log = logging.getLogger(__name__)

_repo = JobsRepository()

# Grace window: only events older than this are reconciled, so the normal
# enqueue-right-after-commit path is left to run first and this never races it.
_GRACE = "15 minutes"
# Cap per tick so one run can't flood the queue; the next tick picks up the rest.
_BATCH = 500


async def reenqueue_stale_events(ctx: dict) -> dict:
    """ARQ cron entrypoint. ``ctx`` is the ARQ job context (unused)."""
    async with get_session() as session:
        stale = await _repo.list_stale_queued_events(
            session, older_than=_GRACE, limit=_BATCH
        )
    for event_id, workspace_id, sweep_id in stale:
        # Thread sweep_id so a sweep event re-runs with sweep_sourced semantics.
        await enqueue(
            "extract_event", workspace_id, event_id, sweep_id,
            _job_id=f"reextract:{event_id}",
        )
    if stale:
        log.info("reenqueue_stale_events: re-enqueued %d stranded events", len(stale))
    return {"reenqueued": len(stale)}
