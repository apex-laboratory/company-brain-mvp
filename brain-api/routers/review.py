from fastapi import APIRouter, HTTPException

router = APIRouter()


@router.get("")
async def list_review_items():
    raise HTTPException(status_code=501, detail="Not implemented — Phase 4")


@router.post("/{item_id}/approve")
async def approve_item(item_id: str):
    raise HTTPException(status_code=501, detail="Not implemented — Phase 4")


@router.post("/{item_id}/reject")
async def reject_item(item_id: str):
    raise HTTPException(status_code=501, detail="Not implemented — Phase 4")
