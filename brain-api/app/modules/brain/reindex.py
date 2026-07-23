"""Keep-fresh hook for the brain index (BRAIN_CHAT_RAG_PLAN Phase 2).

Every publish/approve path that already invalidates the skills cache also asks the
brain index to catch up: enqueue the idempotent ``brain_index_backfill`` job for the
workspace. It only embeds versions that aren't indexed yet and re-syncs
``is_current`` (demoting the superseded version), so it's cheap to fire on every
publish. Best-effort via ``enqueue`` — a queue outage never fails the write.

Imports only the queue (not the brain service), so pipeline/reviews can call it
without creating an import cycle.
"""
from __future__ import annotations

from app.jobs.queue import enqueue


async def schedule_reindex(workspace_id: str) -> None:
    """Enqueue an incremental brain-index backfill for ``workspace_id``."""
    await enqueue("brain_index_backfill", workspace_id)
