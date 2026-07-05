"""``sweep_extract`` job — batched extraction over a sweep's ingested events.

``onboarding_sweep`` ingests historical events (``outcome='queued'``) but defers
extraction to here so it can be rate-limited and progress-tracked as one batch:

* **Order** — events run in authority-priority order (``processing_order()``),
  ``created_at`` breaking ties, so high-authority sources shape the skill set first.
* **Concurrency** — an ``asyncio.Semaphore`` caps concurrent pipeline runs; launches
  are paced at ``rate_per_minute`` (both from ``source_authority.yaml`` ``sweep:``).
* **Sweep-sourced** — every event runs with ``sweep_sourced=True`` so nothing
  auto-publishes (``auto_publish_during_sweep=false``) and boundary search includes
  the sweep's own pending skills.
* **Progress + cost** — a rollup lands in ``sweeps.progress['extraction']``.
* **Resumable** — a crash + ARQ retry re-selects only the still-queued events, so
  finished work is never re-run (per-event finalize is transactional).
"""
from __future__ import annotations

import asyncio
import logging

from app.config.database import get_session
from app.jobs.sweep_order import processing_order
from app.pipeline.authority import sweep_config
from app.pipeline.orchestrator import run_event_safely
from app.pipeline.repository import PipelineRepository
from app.shared.middleware.with_tenant import run_in_tenant

log = logging.getLogger(__name__)

_repo = PipelineRepository()

# Outcome → the sweeps.progress['extraction'] counter it increments.
_COUNTERS = {
    "published": "published",
    "review": "review",
    "draft": "draft",
    "discarded": "discarded",
    "duplicate": "duplicates",
    "contradiction": "contradictions",
    "failed": "failed",
}
_PROGRESS_EVERY = 10  # flush the rollup at most every N processed events


def _new_tally() -> dict[str, int]:
    return {
        "processed": 0, "published": 0, "review": 0, "draft": 0,
        "discarded": 0, "duplicates": 0, "contradictions": 0, "failed": 0,
    }


async def _flush(
    workspace_id: str, sweep_id: str, tally: dict, remaining: int, cost: float
) -> None:
    progress = {**tally, "queued_remaining": remaining, "cost_usd": round(cost, 6)}
    async with get_session() as session, run_in_tenant(
        session, workspace_id, "system", "admin"
    ):
        await _repo.write_extraction_progress(session, sweep_id, progress)
        await session.commit()


async def sweep_extract(ctx: dict, workspace_id: str, sweep_id: str) -> dict:
    """ARQ entrypoint. ``ctx`` is the ARQ job context (unused)."""
    async with get_session() as session, run_in_tenant(
        session, workspace_id, "system", "admin"
    ):
        events = await _repo.list_sweep_queued_events(session, sweep_id)

    if not events:
        log.info("sweep_extract: sweep %s has no queued events", sweep_id)
        return {"processed": 0}

    rank = {provider: i for i, provider in enumerate(processing_order())}
    events.sort(key=lambda e: rank.get(e[1], len(rank)))

    cfg = sweep_config()
    sem = asyncio.Semaphore(max(1, cfg.semaphore_limit))
    launch_delay = 60.0 / cfg.rate_per_minute if cfg.rate_per_minute > 0 else 0.0

    tally = _new_tally()
    total = len(events)
    cost = {"usd": 0.0}
    lock = asyncio.Lock()

    async def worker(event_id: str) -> None:
        async with sem:
            result = await run_event_safely(workspace_id, event_id, sweep_sourced=True)
        async with lock:
            tally["processed"] += 1
            cost["usd"] += result.cost_usd
            counter = _COUNTERS.get(result.outcome)
            if counter:
                tally[counter] += 1
            if tally["processed"] % _PROGRESS_EVERY == 0:
                await _flush(
                    workspace_id, sweep_id, tally, total - tally["processed"], cost["usd"]
                )

    tasks: list[asyncio.Task] = []
    for event_id, _provider in events:
        tasks.append(asyncio.create_task(worker(event_id)))
        if launch_delay:
            await asyncio.sleep(launch_delay)  # pace launches at rate_per_minute
    await asyncio.gather(*tasks)

    await _flush(workspace_id, sweep_id, tally, 0, cost["usd"])
    log.info(
        "sweep_extract: sweep %s processed %d events (%d failed)",
        sweep_id, tally["processed"], tally["failed"],
    )
    return tally
