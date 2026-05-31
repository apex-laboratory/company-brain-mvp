from fastapi import APIRouter, HTTPException
from models.schemas import IngestEvent, SweepConfig

router = APIRouter()


@router.post("/event")
async def ingest_event(event: IngestEvent):
    """
    Webhook receiver for all source events.
    Validates payload, logs to source_events, enqueues for extraction.
    """
    raise HTTPException(status_code=501, detail="Not implemented — Phase 5")


@router.post("/sweep")
async def trigger_sweep(config: SweepConfig):
    """
    Trigger an onboarding or manual sweep.
    Creates a sweeps row and starts the background sweep worker.
    """
    raise HTTPException(status_code=501, detail="Not implemented — Phase 2")


@router.get("/sweep/{sweep_id}/status")
async def sweep_status(sweep_id: str):
    """Return real-time per-source progress for a running or completed sweep."""
    raise HTTPException(status_code=501, detail="Not implemented — Phase 2")
