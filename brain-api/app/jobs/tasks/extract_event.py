"""``extract_event`` job — run the extraction pipeline over one ingested event.

Enqueued by ``webhook_ingest`` / non-sweep ``source_sync`` right after an event
lands (``sweep_extract`` is the batched sweep counterpart). The dead-letter
contract (PRD Phase 3) lives in ``orchestrator.run_event_safely``: transient LLM
errors already retried internally, anything still failing marks the event
``outcome='failed'`` and is swallowed so ARQ's ``retry_jobs`` can't stack on top.
"""
from __future__ import annotations

from app.pipeline.orchestrator import run_event_safely


async def extract_event(
    ctx: dict, workspace_id: str, event_id: str, sweep_id: str | None = None
) -> dict:
    """ARQ entrypoint. ``ctx`` is the ARQ job context (unused)."""
    result = await run_event_safely(
        workspace_id, event_id, sweep_sourced=sweep_id is not None
    )
    return {"outcome": result.outcome, "skill_id": result.skill_id}
