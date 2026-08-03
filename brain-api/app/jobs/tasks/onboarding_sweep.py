"""``onboarding_sweep`` job — historical backfill across connected sources (KAN-2).

Orchestrates one :func:`source_sync` per connected source in authority-priority
order (``source_authority.yaml`` → ``sweep.processing_order``), recording per-source
progress in ``sweeps.progress`` so the onboarding UI can render the "Building your
brain..." screen.

Covers every connected source by default (onboarding), or just the connections in
``sweeps.config.source_ids`` when the sweep was scoped — the per-source historical
import triggered from the Sources page or by a dashboard OAuth connect.

Key properties (PRD Phase 2 acceptance):

* **Failure isolation** — one source failing (revoked token, provider outage) is
  recorded as ``failed`` in that source's progress entry and the sweep moves on;
  it never halts the other sources.
* **Resumable** — if the job itself crashes, the ARQ retry re-enters here and skips
  sources already marked ``completed``; re-synced sources are safe because event
  inserts dedupe and cursors only advance past fetched data.
* **Reuses the sync path** — each source runs through ``source_sync`` (token refresh,
  cursor semantics, auth-vs-transient classification), so the sweep is just the
  first, orchestrated invocation of the same machinery webhooks trigger later.
"""
from __future__ import annotations

import logging

from app.config.database import get_tenant_session
from app.jobs.queue import enqueue
from app.jobs.repository import JobsRepository
from app.jobs.sweep_order import processing_order
from app.jobs.tasks.source_sync import source_sync
from app.shared.middleware.with_tenant import run_in_tenant

log = logging.getLogger(__name__)

_repo = JobsRepository()


async def _write_progress(
    workspace_id: str, sweep_id: str, provider: str, progress: dict
) -> None:
    async with get_tenant_session() as session:
        async with run_in_tenant(session, workspace_id, "system", "admin"):
            await _repo.update_sweep_source_progress(session, sweep_id, provider, progress)
            await session.commit()


async def onboarding_sweep(ctx: dict, workspace_id: str, sweep_id: str) -> dict:
    """ARQ entrypoint. ``ctx`` is the ARQ job context (unused)."""
    async with get_tenant_session() as session:
        async with run_in_tenant(session, workspace_id, "system", "admin"):
            sweep = await _repo.get_sweep(session, sweep_id)
            if sweep is None:
                log.warning("onboarding_sweep: sweep %s not found", sweep_id)
                return {"error": "sweep_not_found"}
            sources = await _repo.list_connected_sources(session)
            await _repo.set_sweep_status(session, sweep_id, "running")
            await session.commit()

    # A scoped sweep (``config.source_ids``) imports only those connections — the
    # per-source backfill from the Sources page or a dashboard OAuth connect. No
    # scope means every connected source, which is the onboarding sweep.
    scope = ((sweep.get("config") or {}).get("source_ids")) or None
    if scope:
        wanted = set(scope)
        sources = [s for s in sources if s["id"] in wanted]

    # On an ARQ retry after a crash, skip sources that already completed.
    prior: dict = sweep.get("progress") or {}
    done = {p for p, v in prior.items() if isinstance(v, dict) and v.get("status") == "completed"}

    rank = {provider: i for i, provider in enumerate(processing_order())}
    ordered = sorted(sources, key=lambda s: rank.get(s["provider"], len(rank)))

    # Progress is keyed by provider; multiple connections of one provider (two
    # Google accounts) aggregate into a single entry.
    progress: dict[str, dict] = {p: dict(v) for p, v in prior.items() if isinstance(v, dict)}
    attempted = failed = 0

    for source in ordered:
        provider, source_id = source["provider"], source["id"]
        if provider in done:
            continue
        attempted += 1
        entry = progress.setdefault(provider, {"status": "running", "inserted": 0})
        entry["status"] = "running"
        await _write_progress(workspace_id, sweep_id, provider, entry)

        try:
            # sweep_id stamps the events and defers extraction to sweep_extract.
            result = await source_sync(ctx, workspace_id, source_id, sweep_id=sweep_id)
        except Exception:
            # Isolation: a transient blow-up in one source must not halt the rest.
            # The held cursor means the next sync (webhook or manual) retries it.
            log.exception("onboarding_sweep: %s (%s) failed", provider, source_id)
            failed += 1
            entry.update(status="failed", error="sync_failed")
            await _write_progress(workspace_id, sweep_id, provider, entry)
            continue

        entry["inserted"] = int(entry.get("inserted", 0)) + int(result.get("inserted", 0))
        if result.get("error"):
            failed += 1
            entry.update(status="failed", error=result["error"])
        else:
            entry["status"] = "completed"
        await _write_progress(workspace_id, sweep_id, provider, entry)

    status = "failed" if attempted and failed == attempted else "completed"
    async with get_tenant_session() as session:
        async with run_in_tenant(session, workspace_id, "system", "admin"):
            await _repo.set_sweep_status(session, sweep_id, status, completed=True)
            await session.commit()

    # Ingestion done → hand the queued events to the batched extraction pass.
    # Enqueue unconditionally: even a partially-failed sweep may have ingested
    # events worth extracting, and sweep_extract no-ops on an empty queue.
    await enqueue("sweep_extract", workspace_id, sweep_id)

    log.info(
        "onboarding_sweep: %s finished (%d sources, %d failed) → %s",
        sweep_id, attempted, failed, status,
    )
    return {"sources": attempted, "failed": failed, "status": status}
