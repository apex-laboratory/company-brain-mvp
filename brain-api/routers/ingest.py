from fastapi import APIRouter, HTTPException

router = APIRouter()


@router.post("/event")
async def ingest_event():
    raise HTTPException(status_code=501, detail="Not implemented — Phase 5")


@router.post("/batch")
async def ingest_batch():
    raise HTTPException(status_code=501, detail="Not implemented — Phase 3")
