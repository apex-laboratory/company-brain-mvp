"""Workspace router.

Mounted under ``/api/v1/workspaces`` by ``main.py``:

* ``POST  /``                          -- create workspace (no workspace context yet)
* ``PATCH /{workspace_id}/onboarding`` -- save onboarding step (admin only)
* ``GET   /{workspace_id}/settings``   -- workspace config + MCP endpoint (any member)
* ``PATCH /{workspace_id}/settings``   -- partial update (admin only)
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.modules.workspaces.schemas import (
    CreateWorkspaceRequest,
    OnboardingPatchRequest,
    WorkspaceSettingsUpdate,
)
from app.modules.workspaces.service import WorkspaceService
from app.shared.errors.app_error import ForbiddenError
from app.shared.http.respond import created, ok
from app.shared.logger import get_logger
from app.shared.middleware.authenticate import AuthContext, get_auth_context
from app.shared.middleware.authorize import require_role
from app.shared.middleware.rate_limit import DASHBOARD_LIMIT, limiter, user_key

log = get_logger()

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


def get_workspace_service() -> WorkspaceService:
    """Provider so the service can be overridden in tests."""
    return WorkspaceService()


@router.post("", status_code=201)
async def create_workspace(
    request: Request,
    body: CreateWorkspaceRequest,
    auth: AuthContext = Depends(get_auth_context),
    service: WorkspaceService = Depends(get_workspace_service),
) -> JSONResponse:
    """Create a workspace for the authenticated user.

    Only a valid JWT is required — the caller has no workspace yet (signup
    token carries workspace_id=None). Returns a new access token scoped to
    the created workspace so the client can make workspace-scoped calls
    immediately without re-signing in.
    """
    result = await service.create_workspace(
        body,
        user_id=auth.user_id,
        user_agent=request.headers.get("user-agent"),
        ip=request.client.host if request.client else None,
    )
    return created(request, result.model_dump(by_alias=True))


@router.patch("/{workspace_id}/onboarding")
async def save_onboarding(
    request: Request,
    workspace_id: str,
    body: OnboardingPatchRequest,
    auth: AuthContext = Depends(get_auth_context),
    _: None = Depends(require_role("admin")),
    service: WorkspaceService = Depends(get_workspace_service),
) -> JSONResponse:
    """Save onboarding progress for a step.

    Caller must be an admin of the target workspace. The workspace_id in the
    JWT is checked against the path param so a token for workspace A cannot
    mutate workspace B even if the role check passes.
    """
    if auth.workspace_id != workspace_id:
        raise ForbiddenError()

    result = await service.save_onboarding_step(
        workspace_id=workspace_id,
        user_id=auth.user_id,
        role=auth.role or "admin",
        request=body,
    )
    return ok(request, result.model_dump(by_alias=True))


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
    fields = body.model_dump(exclude_unset=True)
    payload = await service.update_settings(auth, workspace_id, fields)
    return ok(request, payload.model_dump(by_alias=True))
