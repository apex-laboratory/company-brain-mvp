import asyncio
import json
from uuid import UUID

from fastapi import APIRouter, HTTPException

from database import get_pool
from models.schemas import IngestEvent, SweepConfig, SweepSourceProgress, SweepStatus
from services.sweep_worker import run_sweep

router = APIRouter()


@router.post("/event", status_code=200)
async def ingest_event(event: IngestEvent):
    """
    Webhook receiver for all source events.
    Logs to source_events with processed=FALSE; pipeline picks it up in Phase 3/5.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        event_id = await conn.fetchval(
            "INSERT INTO source_events (source, event_type, source_id, payload) "
            "VALUES ($1, $2, $3, $4::jsonb) RETURNING id",
            event.source, event.event_type, event.source_id, json.dumps(event.payload),
        )
    return {"id": str(event_id), "status": "queued"}


@router.post("/sweep", response_model=SweepStatus, status_code=202)
async def trigger_sweep(config: SweepConfig):
    """
    Trigger an onboarding or manual sweep.
    Creates a sweeps row and starts the background sweep worker.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO sweeps (config) VALUES ($1::jsonb) "
            "RETURNING id, status, progress, skills_created, skills_queued, "
            "started_at, completed_at",
            json.dumps(config.model_dump()),
        )
    asyncio.create_task(run_sweep(row["id"]))
    return SweepStatus(
        id=row["id"],
        status=row["status"],
        sources={},
        skills_created=row["skills_created"],
        skills_queued=row["skills_queued"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
    )


@router.get("/sweep/{sweep_id}/status", response_model=SweepStatus)
async def sweep_status(sweep_id: UUID):
    """Return real-time per-source progress for a running or completed sweep."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, status, progress, skills_created, skills_queued, "
            "started_at, completed_at FROM sweeps WHERE id=$1",
            sweep_id,
        )
    if row is None:
        raise HTTPException(status_code=404, detail="Sweep not found")
    sources = {
        src: SweepSourceProgress(**counts)
        for src, counts in (row["progress"] or {}).items()
    }
    return SweepStatus(
        id=row["id"],
        status=row["status"],
        sources=sources,
        skills_created=row["skills_created"],
        skills_queued=row["skills_queued"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
    )
