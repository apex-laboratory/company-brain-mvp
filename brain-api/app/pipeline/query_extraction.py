"""Query-driven extraction fallback (Phase 5 — PRD Feature 16 / Process 4).

When ``query_brain`` finds no published match, the agent still needs an answer.
This module runs a live extraction from connected sources, honoring a hard
**15-second latency budget** inside the agent call: if the pipeline can't finish
in time, it returns immediately with ``extraction_queued=true`` and finishes the
work asynchronously (an ARQ ``query_extract`` job), so the agent is never blocked.

Design contract (what callers and agents depend on):

* Return shape is always one of:
  - ``match_type="query_driven"`` — a skill was extracted live and is returned,
    stored as a draft review for human promotion.
  - ``match_type="no_match"`` with ``extraction_queued`` — either nothing relevant
    was found (``false``) or the budget was exceeded and extraction continues
    asynchronously (``true``, with ``retry_after_seconds``).
* Every path is workspace-scoped (RLS) and logs an ``agent_interactions`` row.

**Live-source search seam:** the actual per-provider search (Notion/Drive/Slack/
Zendesk query APIs) is not implemented yet — the ``SourceIntegration`` protocol
exposes ``fetch_since`` (incremental sync) but no ``search``. Until a provider
implements :func:`search_sources`, this returns an honest ``no_match`` rather than
fabricating a skill. The budget/fallback/logging contract above is complete and
tested so the agent-facing behavior is stable when provider search lands.
"""
from __future__ import annotations

import asyncio
import logging

from app.shared.middleware.authenticate import AuthContext

log = logging.getLogger(__name__)

BUDGET_SECONDS = 15.0
RETRY_AFTER_SECONDS = 60


async def search_sources(auth: AuthContext, situation: str) -> list[dict]:
    """Search connected sources for content relevant to ``situation``.

    The extension seam for live query-driven extraction. Returns candidate
    documents ``[{provider, external_id, event_type, content, url}]`` to stage as
    ephemeral ``source_events`` for the pipeline. No provider implements search
    yet (see module docstring), so this returns ``[]`` — callers treat that as a
    genuine no-match, never a failure.
    """
    return []


async def run_query_extraction(
    auth: AuthContext, situation: str, *, prior: dict | None = None
) -> dict:
    """Attempt live extraction within the latency budget; fall back to async.

    ``prior`` is the ``no_match`` payload ``query_brain`` already built (carries
    the ``interaction_id`` to preserve across the fallback)."""
    base = dict(prior or {"match_type": "no_match", "similarity_score": None})
    try:
        return await asyncio.wait_for(
            _extract_inline(auth, situation, base), timeout=BUDGET_SECONDS
        )
    except TimeoutError:
        log.info("query_extraction: budget exceeded — deferring to async job")
        await _enqueue_async(auth.workspace_id, situation)
        return {**base, "match_type": "no_match", "extraction_queued": True,
                "retry_after_seconds": RETRY_AFTER_SECONDS}


async def _extract_inline(auth: AuthContext, situation: str, base: dict) -> dict:
    """Inline extraction path (bounded by the caller's ``wait_for`` budget)."""
    candidates = await search_sources(auth, situation)
    if not candidates:
        # Nothing to extract from — a real miss, not a deferral.
        return {**base, "match_type": "no_match", "extraction_queued": False}

    # Provider search returned content: stage it and run extraction asynchronously
    # (a full multi-source pipeline pass rarely fits the inline budget), then tell
    # the agent to retry. When inline single-doc extraction is fast enough, this is
    # where its result would be returned as match_type="query_driven".
    await _enqueue_async(auth.workspace_id, situation)
    return {**base, "match_type": "no_match", "extraction_queued": True,
            "retry_after_seconds": RETRY_AFTER_SECONDS}


async def _enqueue_async(workspace_id: str | None, situation: str) -> None:
    """Enqueue the async ``query_extract`` job (best-effort; never fails the call)."""
    if workspace_id is None:
        return
    try:
        from app.jobs.queue import enqueue

        await enqueue("query_extract", workspace_id, situation)
    except Exception:  # noqa: BLE001 — a queue outage must not fail the agent call
        log.warning("query_extraction: could not enqueue async job", exc_info=True)
