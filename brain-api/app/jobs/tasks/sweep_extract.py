"""``sweep_extract`` job — batched extraction over a sweep's ingested events.

``onboarding_sweep`` ingests historical events (``outcome='queued'``) but defers
extraction to here so it can be rate-limited and progress-tracked as one batch:

* **Order** — events run in authority-priority order (``processing_order()``),
  ``created_at`` breaking ties, so high-authority sources shape the skill set first.
* **Concurrency** — an ``asyncio.Semaphore`` caps concurrent pipeline runs; launches
  are paced at ``rate_per_minute`` (both from ``source_authority.yaml`` ``sweep:``).
* **Sweep-sourced** — every event runs with ``sweep_sourced=True`` so nothing
  auto-publishes (``skill_writer.route`` never publishes a sweep-sourced draft) and
  boundary search includes the sweep's own pending skills.
* **Progress + cost** — a rollup lands in ``sweeps.progress['extraction']``.
* **Bounded + resumable** — each invocation launches at most ``_MAX_PER_RUN``
  events (so the paced launch loop stays under the job timeout even at a slow
  ``rate_per_minute``) and re-enqueues a continuation while any remain. A crash +
  ARQ retry re-selects only the still-queued events, so finished work is never
  re-run (per-event finalize is transactional).
"""
from __future__ import annotations

import asyncio
import logging

from app.config.database import get_session
from app.jobs.queue import enqueue
from app.jobs.sweep_order import processing_order
from app.pipeline.authority import sweep_config
from app.pipeline.orchestrator import run_event_safely
from app.pipeline.repository import PipelineRepository
from app.pipeline.types import TERMINAL_OUTCOMES
from app.shared.middleware.with_tenant import run_in_tenant

log = logging.getLogger(__name__)

_repo = PipelineRepository()

# Cap events launched per invocation so the paced launch loop stays under the
# job timeout even at a slow rate_per_minute; a continuation handles the rest.
_MAX_PER_RUN = 300

# Outcome → the sweeps.progress['extraction'] counter it increments. Keyed by the
# canonical terminal-outcome set so adding a new outcome without a counter here
# fails at import (the assert), not silently at runtime.
_COUNTERS = {
    "published": "published",
    "review": "review",
    "draft": "draft",
    "discarded": "discarded",
    "duplicate": "duplicates",
    "contradiction": "contradictions",
    "failed": "failed",
}
assert set(_COUNTERS) == set(TERMINAL_OUTCOMES), (
    "sweep_extract._COUNTERS is out of sync with pipeline.types.TERMINAL_OUTCOMES"
)
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
        events = await _repo.list_sweep_queued_events(session, sweep_id, limit=_MAX_PER_RUN)

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

    # Events beyond this run's cap (or newly ingested by a still-chaining backfill)
    # remain queued; report the true remaining count and chain a continuation so the
    # sweep finishes without any single job exceeding its timeout.
    async with get_session() as session, run_in_tenant(
        session, workspace_id, "system", "admin"
    ):
        remaining = await _repo.count_sweep_queued_events(session, sweep_id)
    await _flush(workspace_id, sweep_id, tally, remaining, cost["usd"])
    if remaining:
        # No stable _job_id: this is the sole continuation producer, and reusing an
        # id would collide with this finishing job's own retained result and stall
        # the chain. The pipeline's processed-guard keeps a stray re-run harmless.
        await enqueue("sweep_extract", workspace_id, sweep_id)
    log.info(
        "sweep_extract: sweep %s processed %d events (%d failed, %d remaining)",
        sweep_id, tally["processed"], tally["failed"], remaining,
    )
    return {**tally, "queued_remaining": remaining}
