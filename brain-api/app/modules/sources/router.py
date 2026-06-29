"""Source-integration routers (API_DOCUMENTATION.md §Source Integrations API).

Two routers, both mounted under ``/api/v1`` by ``main.py``:

* ``catalog_router`` (``/sources``) — the static provider catalog, any member.
* ``router`` (``/workspaces``) — workspace-scoped connect/callback/list/scope/
  disconnect (admin-only, gated by ``connections_admin`` RLS) plus the channel
  list (any member, gated by ``channels_select`` RLS).

Connect and callback run OAuth round-trips, so they carry the tighter OAuth-callback
limit; the rest use the dashboard surface limit. All keyed per user.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from app.integrations.source_oauth import PROVIDERS
from app.modules.sources.schemas import (
    SourceCallbackRequest,
    SourceConnectRequest,
    SourceScopeRequest,
)
from app.modules.sources.service import SourceService
from app.shared.http.respond import created, error_response, no_content, ok
from app.shared.middleware.authenticate import AuthContext, get_auth_context
from app.shared.middleware.authorize import require_role
from app.shared.middleware.rate_limit import (
    DASHBOARD_LIMIT,
    OAUTH_CALLBACK_LIMIT,
    limiter,
    user_key,
)

catalog_router = APIRouter(prefix="/sources", tags=["sources"])
router = APIRouter(prefix="/workspaces", tags=["sources"])

_INVALID_PROVIDER = "Provider must be one of: slack, notion, github, jira, zendesk."


def get_source_service() -> SourceService:
    """Provider so the service can be overridden in tests."""
    return SourceService()


@catalog_router.get("/providers")
@limiter.limit(DASHBOARD_LIMIT, key_func=user_key)
async def list_providers(
    request: Request,
    _auth: AuthContext = Depends(get_auth_context),
    service: SourceService = Depends(get_source_service),
) -> JSONResponse:
    providers = service.list_providers()
    return ok(request, [p.model_dump(by_alias=True) for p in providers])


@router.post("/{workspace_id}/sources/{provider}/connect")
@limiter.limit(OAUTH_CALLBACK_LIMIT, key_func=user_key)
async def connect_source(
    request: Request,
    workspace_id: str,
    provider: str,
    body: SourceConnectRequest,
    auth: AuthContext = Depends(get_auth_context),
    _: None = Depends(require_role("admin")),
    service: SourceService = Depends(get_source_service),
) -> JSONResponse:
    if provider not in PROVIDERS:
        return error_response(
            request, status=400, code="invalid_provider", message=_INVALID_PROVIDER
        )
    result = await service.start_connect(auth, workspace_id, provider, body)
    return ok(request, result.model_dump(by_alias=True))


@router.post("/{workspace_id}/sources/{provider}/callback", status_code=201)
@limiter.limit(OAUTH_CALLBACK_LIMIT, key_func=user_key)
async def source_callback(
    request: Request,
    workspace_id: str,
    provider: str,
    body: SourceCallbackRequest,
    auth: AuthContext = Depends(get_auth_context),
    _: None = Depends(require_role("admin")),
    service: SourceService = Depends(get_source_service),
) -> JSONResponse:
    if provider not in PROVIDERS:
        return error_response(
            request, status=400, code="invalid_provider", message=_INVALID_PROVIDER
        )
    source = await service.handle_callback(auth, workspace_id, provider, body)
    return created(request, {"source": source.model_dump(by_alias=True)})


@router.get("/{workspace_id}/sources")
@limiter.limit(DASHBOARD_LIMIT, key_func=user_key)
async def list_sources(
    request: Request,
    workspace_id: str,
    auth: AuthContext = Depends(get_auth_context),
    _: None = Depends(require_role("admin")),
    service: SourceService = Depends(get_source_service),
) -> JSONResponse:
    sources = await service.list_sources(auth, workspace_id)
    return ok(request, [s.model_dump(by_alias=True) for s in sources])


@router.put("/{workspace_id}/sources/scope")
@limiter.limit(DASHBOARD_LIMIT, key_func=user_key)
async def configure_scope(
    request: Request,
    workspace_id: str,
    body: SourceScopeRequest,
    auth: AuthContext = Depends(get_auth_context),
    _: None = Depends(require_role("admin")),
    service: SourceService = Depends(get_source_service),
) -> JSONResponse:
    result = await service.update_scope(auth, workspace_id, body)
    return ok(request, result.model_dump(by_alias=True))


@router.get("/{workspace_id}/sources/{source_id}/channels")
@limiter.limit(DASHBOARD_LIMIT, key_func=user_key)
async def list_channels(
    request: Request,
    workspace_id: str,
    source_id: str,
    auth: AuthContext = Depends(get_auth_context),
    service: SourceService = Depends(get_source_service),
) -> JSONResponse:
    channels = await service.list_channels(auth, workspace_id, source_id)
    return ok(request, [c.model_dump(by_alias=True) for c in channels])


@router.delete("/{workspace_id}/sources/{source_id}", status_code=204)
@limiter.limit(DASHBOARD_LIMIT, key_func=user_key)
async def disconnect_source(
    request: Request,
    workspace_id: str,
    source_id: str,
    auth: AuthContext = Depends(get_auth_context),
    _: None = Depends(require_role("admin")),
    service: SourceService = Depends(get_source_service),
) -> Response:
    await service.disconnect(auth, workspace_id, source_id)
    return no_content()
