"""Decisions registry router (BACKEND_ASKS §8).

Mounted under ``/api/v1``. Read-only, dashboard-only (JWT, role ≥ viewer):

* ``GET /decisions``       — paginated, filterable list.
* ``GET /decisions/{id}``  — one decision with owner + executable rule.

RLS scopes every read in the service; the role gate is defense in depth.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from app.modules.decisions.service import DecisionsService
from app.shared.http.respond import ok
from app.shared.middleware.authenticate import AuthContext, get_auth_context
from app.shared.middleware.authorize import require_role
from app.shared.middleware.rate_limit import DASHBOARD_LIMIT, limiter, user_key

router = APIRouter(prefix="/decisions", tags=["decisions"])

_service = DecisionsService()

_Status = Annotated[str | None, Query(pattern="^(approved|active|review)$")]


@router.get("", dependencies=[Depends(require_role("viewer"))])
@limiter.limit(DASHBOARD_LIMIT, key_func=user_key)
async def list_decisions(
    request: Request,
    status: _Status = None,
    category: Annotated[str | None, Query(max_length=100)] = None,
    source: Annotated[str | None, Query(max_length=40)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
    auth: AuthContext = Depends(get_auth_context),
):
    """List decisions, newest-updated first; filterable by status/category/source."""
    items, next_cursor = await _service.list(
        auth, status=status, category=category, source=source, limit=limit, cursor=cursor
    )
    return ok(
        request,
        [i.model_dump(by_alias=True) for i in items],
        nextCursor=next_cursor,
    )


@router.get("/{decision_id}", dependencies=[Depends(require_role("viewer"))])
@limiter.limit(DASHBOARD_LIMIT, key_func=user_key)
async def get_decision(
    decision_id: str,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    """One decision with full body + executable rule."""
    decision = await _service.get(auth, decision_id)
    return ok(request, decision.model_dump(by_alias=True))
