"""Workspace settings router (KAN-64).

Mounted under ``/api/v1/workspaces`` by ``main.py``:

* ``GET   /{workspace_id}/settings``  — workspace config + MCP endpoint (any member).
* ``PATCH /{workspace_id}/settings``  — partial update (admin only).

Both require an authenticated workspace member (``get_auth_context`` →
service-layer membership check; non-members get 403) and are rate limited to the
dashboard surface (300/min keyed on the user, per API_DOCUMENTATION.md). The
update additionally requires the ``admin`` role.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.modules.workspaces.schemas import WorkspaceSettingsUpdate
from app.modules.workspaces.service import WorkspaceService
from app.shared.http.respond import ok
from app.shared.middleware.authenticate import AuthContext, get_auth_context
from app.shared.middleware.authorize import require_role
from app.shared.middleware.rate_limit import DASHBOARD_LIMIT, limiter, user_key

router = APIRouter(prefix="/workspaces", tags=["settings"])


def get_workspace_service() -> WorkspaceService:
    """Provider so the service can be overridden in tests."""
    return WorkspaceService()


@router.get("/{workspace_id}/settings")
@limiter.limit(DASHBOARD_LIMIT, key_func=user_key)
async def get_settings(
    request: Request,
    workspace_id: str,
    auth: AuthContext = Depends(get_auth_context),
    service: WorkspaceService = Depends(get_workspace_service),
) -> JSONResponse:
    payload = await service.get_settings(auth, workspace_id)
    return ok(request, payload.model_dump(by_alias=True))


@router.patch("/{workspace_id}/settings")
@limiter.limit(DASHBOARD_LIMIT, key_func=user_key)
async def update_settings(
    request: Request,
    workspace_id: str,
    body: WorkspaceSettingsUpdate,
    auth: AuthContext = Depends(get_auth_context),
    _: None = Depends(require_role("admin")),
    service: WorkspaceService = Depends(get_workspace_service),
) -> JSONResponse:
    # exclude_unset → only the fields the client actually sent are updated.
    fields = body.model_dump(exclude_unset=True)
    payload = await service.update_settings(auth, workspace_id, fields)
    return ok(request, payload.model_dump(by_alias=True))
