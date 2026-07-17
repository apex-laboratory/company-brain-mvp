"""Reviews router (BACKEND_BEST_PRACTICES.md §2, §6).

Paths + dependencies only — logic lives in :class:`ReviewsService`. Mounted under
``/api/v1`` by ``main.py``. Admin-only, like sweeps: reviewing published skills
is a privileged action.

* ``GET  /reviews`` — the human review queue (contradictions first), filterable.
* ``GET  /reviews/stats`` — approve/reject counts + rejection rate (PRD §15).
* ``POST /reviews/{id}/approve`` — apply the extracted change, publish the skill.
* ``POST /reviews/{id}/reject`` — record the verdict; demote a review-only skill.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from app.modules.reviews.schemas import ResolveRequest
from app.modules.reviews.service import ReviewsService
from app.shared.http.respond import ok
from app.shared.middleware.authenticate import AuthContext, get_auth_context
from app.shared.middleware.authorize import require_role

router = APIRouter(prefix="/reviews", tags=["reviews"])

_service = ReviewsService()

_Status = Annotated[str | None, Query(pattern="^(pending|approved|rejected)$")]
_Kind = Annotated[
    str | None, Query(pattern="^(policy_change|new_decision|contradiction|exception)$")
]


@router.get("", dependencies=[Depends(require_role("admin"))])
async def list_reviews(
    request: Request,
    status: _Status = None,
    kind: _Kind = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    auth: AuthContext = Depends(get_auth_context),
):
    """List reviews, newest first (contradictions surface first)."""
    reviews = await _service.list(auth, status=status, kind=kind, limit=limit)
    return ok(request, [r.model_dump(by_alias=True) for r in reviews])


@router.get("/stats", dependencies=[Depends(require_role("admin"))])
async def review_stats(request: Request, auth: AuthContext = Depends(get_auth_context)):
    """Approve/reject counts + rejection rate."""
    stats = await _service.stats(auth)
    return ok(request, stats.model_dump(by_alias=True))


@router.post("/{review_id}/approve", dependencies=[Depends(require_role("admin"))])
async def approve_review(
    review_id: str,
    request: Request,
    body: ResolveRequest | None = None,
    auth: AuthContext = Depends(get_auth_context),
):
    """Approve a review: apply the extracted change and publish the skill."""
    result = await _service.approve(auth, review_id, (body or ResolveRequest()).comment)
    return ok(request, result.model_dump(by_alias=True))


@router.post("/{review_id}/reject", dependencies=[Depends(require_role("admin"))])
async def reject_review(
    review_id: str,
    request: Request,
    body: ResolveRequest | None = None,
    auth: AuthContext = Depends(get_auth_context),
):
    """Reject a review: record the verdict; demote a review-only skill to draft."""
    result = await _service.reject(auth, review_id, (body or ResolveRequest()).comment)
    return ok(request, result.model_dump(by_alias=True))
