"""``query_extract`` job — async completion of a query-driven extraction (Feature 16).

Enqueued by ``app.pipeline.query_extraction`` when the inline 15-second budget is
exceeded (or is deferred by design), so ``query_brain`` can return immediately and
the extraction finishes out of band. The agent retries after ``retry_after_seconds``
and gets the now-published/queued skill via the normal semantic path.

Currently a no-op beyond logging: the live per-provider search that would stage
``source_events`` for this query is not implemented yet (see
``query_extraction.search_sources``). Registered now so the enqueue target exists
and the async contract is stable when provider search lands.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


async def query_extract(ctx: dict, workspace_id: str, situation: str) -> dict:
    """ARQ entrypoint. ``ctx`` is the ARQ job context (unused)."""
    log.info(
        "query_extract: async extraction requested for workspace=%s (no-op until "
        "provider search is implemented)",
        workspace_id,
    )
    return {"outcome": "noop", "workspace_id": workspace_id}
