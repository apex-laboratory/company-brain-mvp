from fastapi import APIRouter, HTTPException
from models.schemas import ReviewAction, ReviewWrite, BulkApproveRequest

router = APIRouter()


@router.get("")
async def list_review_items():
    """
    List pending review items grouped by type.
    Order: contradiction → update → exception → new → query_driven → sweep_sourced.
    """
    raise HTTPException(status_code=501, detail="Not implemented — Phase 4")


@router.get("/{item_id}")
async def get_review_item(item_id: str):
    """Return a single review item with full source context."""
    raise HTTPException(status_code=501, detail="Not implemented — Phase 4")


@router.post("/{item_id}/approve")
async def approve_item(item_id: str, action: ReviewAction):
    """Approve and publish the proposed skill update."""
    raise HTTPException(status_code=501, detail="Not implemented — Phase 4")


@router.post("/{item_id}/reject")
async def reject_item(item_id: str, action: ReviewAction):
    """Reject the proposed skill update."""
    raise HTTPException(status_code=501, detail="Not implemented — Phase 4")


@router.post("/{item_id}/write")
async def write_correction(item_id: str, correction: ReviewWrite):
    """
    Human writes the correct skill version directly.
    Sets changed_by=human_authored, confidence=1.0, auto-publishes.
    """
    raise HTTPException(status_code=501, detail="Not implemented — Phase 4")


@router.post("/bulk-approve")
async def bulk_approve(request: BulkApproveRequest):
    """
    Approve a list of sweep-sourced items at once.
    Used for cluster-based bulk review of onboarding sweep results.
    """
    raise HTTPException(status_code=501, detail="Not implemented — Phase 4")
