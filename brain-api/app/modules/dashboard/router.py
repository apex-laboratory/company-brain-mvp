"""Dashboard router (KAN-58).

Mounted under ``/api/v1/workspaces`` by ``main.py``. Two read-only endpoints
that power the home screen:

* ``GET /{workspace_id}/overview``  — aggregated dashboard payload.
* ``GET /{workspace_id}/activity``  — cursor-paginated activity feed.

Both require an authenticated workspace member (``get_auth_context`` →
service-layer membership check; non-members get 403) and are rate limited to the
dashboard surface (300/min keyed on the user, per API_DOCUMENTATION.md).
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from app.modules.dashboard.schemas import ActivityQuery
from app.modules.dashboard.service import DashboardService
from app.shared.http.respond import ok
from app.shared.middleware.authenticate import AuthContext, get_auth_context
from app.shared.middleware.rate_limit import DASHBOARD_LIMIT, limiter, user_key

router = APIRouter(prefix="/workspaces", tags=["dashboard"])


def get_dashboard_service() -> DashboardService:
    """Provider so the service can be overridden in tests."""
    return DashboardService()


@router.get("/{workspace_id}/overview")
@limiter.limit(DASHBOARD_LIMIT, key_func=user_key)
async def get_overview(
    request: Request,
    workspace_id: str,
    auth: AuthContext = Depends(get_auth_context),
    service: DashboardService = Depends(get_dashboard_service),
) -> JSONResponse:
    overview = await service.get_overview(auth, workspace_id)
    return ok(request, overview.model_dump(by_alias=True))


@router.get("/{workspace_id}/usage")
@limiter.limit(DASHBOARD_LIMIT, key_func=user_key)
async def get_usage(
    request: Request,
    workspace_id: str,
    auth: AuthContext = Depends(get_auth_context),
    service: DashboardService = Depends(get_dashboard_service),
) -> JSONResponse:
    """Measured usage counters — settings Usage tab + sidebar meter."""
    usage = await service.get_usage(auth, workspace_id)
    return ok(request, usage.model_dump(by_alias=True))


@router.get("/{workspace_id}/activity")
@limiter.limit(DASHBOARD_LIMIT, key_func=user_key)
async def get_activity(
    request: Request,
    workspace_id: str,
    query: Annotated[ActivityQuery, Query()],
    auth: AuthContext = Depends(get_auth_context),
    service: DashboardService = Depends(get_dashboard_service),
) -> JSONResponse:
    page = await service.get_activity(auth, workspace_id, query)
    data = [event.model_dump(by_alias=True) for event in page.events]
    return ok(request, data, nextCursor=page.next_cursor)
