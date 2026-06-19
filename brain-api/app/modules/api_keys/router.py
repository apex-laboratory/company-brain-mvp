"""Workspace API key router (KAN-64).

Mounted under ``/api/v1/workspaces`` by ``main.py``:

* ``GET    /{workspace_id}/api-keys``          — list keys (prefix only).
* ``POST   /{workspace_id}/api-keys``          — create a key, reveal raw key once.
* ``DELETE /{workspace_id}/api-keys/{key_id}`` — revoke a key immediately (204).

All three require the ``admin`` role (``require_role("admin")``) — the
``api_keys_admin`` RLS policy also gates the rows to workspace admins. Creation is
additionally rate limited to 5/hour per admin (API_DOCUMENTATION.md); the other
routes use the dashboard surface limit (300/min per user).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from app.modules.api_keys.schemas import ApiKeyCreateRequest
from app.modules.api_keys.service import ApiKeyService
from app.shared.http.respond import created, no_content, ok
from app.shared.middleware.authenticate import AuthContext, get_auth_context
from app.shared.middleware.authorize import require_role
from app.shared.middleware.rate_limit import (
    API_KEY_CREATE_LIMIT,
    DASHBOARD_LIMIT,
    limiter,
    user_key,
)

router = APIRouter(prefix="/workspaces", tags=["api-keys"])


def get_api_key_service() -> ApiKeyService:
    """Provider so the service can be overridden in tests."""
    return ApiKeyService()


@router.get("/{workspace_id}/api-keys")
@limiter.limit(DASHBOARD_LIMIT, key_func=user_key)
async def list_api_keys(
    request: Request,
    workspace_id: str,
    auth: AuthContext = Depends(get_auth_context),
    _: None = Depends(require_role("admin")),
    service: ApiKeyService = Depends(get_api_key_service),
) -> JSONResponse:
    keys = await service.list_keys(auth, workspace_id)
    return ok(request, [key.model_dump(by_alias=True) for key in keys])


@router.post("/{workspace_id}/api-keys", status_code=201)
@limiter.limit(API_KEY_CREATE_LIMIT, key_func=user_key)
async def create_api_key(
    request: Request,
    workspace_id: str,
    body: ApiKeyCreateRequest,
    auth: AuthContext = Depends(get_auth_context),
    _: None = Depends(require_role("admin")),
    service: ApiKeyService = Depends(get_api_key_service),
) -> JSONResponse:
    key = await service.create_key(auth, workspace_id, body)
    return created(request, key.model_dump(by_alias=True))


@router.delete("/{workspace_id}/api-keys/{key_id}", status_code=204)
@limiter.limit(DASHBOARD_LIMIT, key_func=user_key)
async def revoke_api_key(
    request: Request,
    workspace_id: str,
    key_id: str,
    auth: AuthContext = Depends(get_auth_context),
    _: None = Depends(require_role("admin")),
    service: ApiKeyService = Depends(get_api_key_service),
) -> Response:
    await service.revoke_key(auth, workspace_id, key_id)
    return no_content()
