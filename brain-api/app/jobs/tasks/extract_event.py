"""``extract_event`` job — run the extraction pipeline over one ingested event.

Enqueued by ``webhook_ingest`` / ``source_sync`` right after an event lands
(``sweep_extract`` — M3 — is the batched sweep counterpart). Failure contract
(PRD Phase 3): transient LLM errors already retried inside ``with_retries``;
anything that still escapes marks the event ``outcome='failed'`` (dead-letter,
attempts+1, error recorded) and is **swallowed** — ARQ's ``retry_jobs`` must not
triple the LLM retries. Failed events are re-runnable by resetting
``processed=false, outcome='queued'`` and re-enqueueing.
"""
from __future__ import annotations

import logging

from app.config.database import get_session
from app.pipeline.orchestrator import run_pipeline
from app.pipeline.repository import PipelineRepository
from app.shared.middleware.with_tenant import run_in_tenant

log = logging.getLogger(__name__)

_repo = PipelineRepository()


async def extract_event(
    ctx: dict, workspace_id: str, event_id: str, sweep_id: str | None = None
) -> dict:
    """ARQ entrypoint. ``ctx`` is the ARQ job context (unused)."""
    sweep_sourced = sweep_id is not None
    try:
        result = await run_pipeline(workspace_id, event_id, sweep_sourced=sweep_sourced)
    except Exception as exc:  # noqa: BLE001 — dead-letter by contract, never re-raise
        log.exception("extract_event: %s failed — dead-lettered", event_id)
        try:
            async with get_session() as session, run_in_tenant(
                session, workspace_id, "system", "admin"
            ):
                await _repo.finalize_event(
                    session,
                    event_id,
                    outcome="failed",
                    pipeline_meta={"error": f"{type(exc).__name__}: {exc}"},
                )
                await session.commit()
        except Exception:  # noqa: BLE001
            log.exception("extract_event: could not record failure for %s", event_id)
        return {"outcome": "failed"}
    return {"outcome": result.outcome, "skill_id": result.skill_id}
