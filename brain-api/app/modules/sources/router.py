"""Sources router (BACKEND_BEST_PRACTICES.md §2, §6).

Paths + dependencies only — all logic lives in :class:`SourcesService`. Mounted
under ``/api/v1`` by ``main.py``.

All connection routes require an **admin** dashboard JWT (``source_connections``
is admin-only at the RLS layer). The OAuth *callback* is exempt: it is a browser
redirect from the provider that carries no JWT, so it authenticates via the
signed, single-use ``state`` instead.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from app.modules.sources.schemas import AuthorizeStartRequest, ChannelSelectRequest
from app.modules.sources.service import SourcesService
from app.shared.http.respond import accepted, no_content, ok
from app.shared.middleware.authenticate import AuthContext, get_auth_context
from app.shared.middleware.authorize import require_role
from app.shared.middleware.rate_limit import (
    DASHBOARD_LIMIT,
    OAUTH_CALLBACK_LIMIT,
    limiter,
    user_key,
)

router = APIRouter(prefix="/sources", tags=["sources"])

_service = SourcesService()


@router.get("", dependencies=[Depends(require_role("admin"))])
async def list_sources(request: Request, auth: AuthContext = Depends(get_auth_context)):
    """List this workspace's source connections."""
    connections = await _service.list_connections(auth)
    return ok(request, [c.model_dump(by_alias=True) for c in connections])


@router.post("/{provider}/authorize", dependencies=[Depends(require_role("admin"))])
async def authorize(
    provider: str,
    request: Request,
    body: AuthorizeStartRequest | None = None,
    auth: AuthContext = Depends(get_auth_context),
):
    """Begin the OAuth flow: return the provider consent URL to redirect the user to.

    Subdomain-scoped providers (Zendesk) pass ``{"subdomain": "acme"}`` in the body.
    ``returnTo`` (optional, allowlisted) picks the frontend path the callback redirects
    to — e.g. ``/onboarding`` — instead of the default sources page.
    """
    result = await _service.start_authorization(
        auth,
        provider,
        subdomain=body.subdomain if body else None,
        return_to=body.return_to if body else None,
    )
    return accepted(request, result.model_dump(by_alias=True))


@router.get("/{provider}/callback")
@limiter.limit(OAUTH_CALLBACK_LIMIT)
async def callback(
    provider: str,
    request: Request,
    state: str,
    code: str | None = None,
    installation_id: str | None = None,
    error: str | None = None,
):
    """OAuth redirect target: exchange the code, store the connection, bounce to the dashboard.

    ``code`` is optional because a pure GitHub App install redirects here with an
    ``installation_id`` and no ``code``. ``error`` is set when the user declines the
    consent screen (e.g. ``error=access_denied``); the service redirects back cleanly
    instead of trying to exchange a missing code.
    """
    redirect_to = await _service.handle_callback(
        provider, state=state, code=code, installation_id=installation_id, error=error
    )
    return RedirectResponse(url=redirect_to, status_code=302)


@router.get("/{source_id}/report", dependencies=[Depends(require_role("admin"))])
@limiter.limit(DASHBOARD_LIMIT, key_func=user_key)
async def read_report(
    source_id: str, request: Request, auth: AuthContext = Depends(get_auth_context)
):
    """What this source read, what became knowledge, and why the rest didn't.

    A successful import that yields no skills is indistinguishable from a broken one
    unless the discard reasons are visible — the pipeline records them per event, and
    this is the surface that shows them.
    """
    report = await _service.get_report(auth, source_id)
    return ok(request, report.model_dump(by_alias=True))


@router.post("/{source_id}/backfill", dependencies=[Depends(require_role("admin"))])
@limiter.limit(DASHBOARD_LIMIT, key_func=user_key)
async def backfill(
    source_id: str, request: Request, auth: AuthContext = Depends(get_auth_context)
):
    """Import this source's history (a sweep scoped to one connection).

    ``202`` for a newly started import, ``200`` when one covering this source was
    already in flight — the same created/existing split as ``POST /sweeps``, and the
    same ``SweepOut`` body, so a caller can poll ``GET /sweeps/{id}`` either way.
    """
    sweep, created = await _service.start_backfill(auth, source_id)
    payload = sweep.model_dump(by_alias=True)
    return accepted(request, payload) if created else ok(request, payload)


@router.post("/{source_id}/disconnect", dependencies=[Depends(require_role("admin"))])
async def disconnect(source_id: str, auth: AuthContext = Depends(get_auth_context)):
    """Revoke (best-effort) and delete a source connection."""
    await _service.disconnect(auth, source_id)
    return no_content()


@router.get("/{source_id}/channels", dependencies=[Depends(require_role("admin"))])
async def list_channels(
    source_id: str, request: Request, auth: AuthContext = Depends(get_auth_context)
):
    """List provider-discoverable channels merged with persisted selection state."""
    channels = await _service.list_channels(auth, source_id)
    return ok(request, [c.model_dump(by_alias=True) for c in channels])


@router.patch("/{source_id}/channels", dependencies=[Depends(require_role("admin"))])
async def select_channels(
    source_id: str,
    body: ChannelSelectRequest,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    """Toggle which channels are selected for ingestion."""
    channels = await _service.select_channels(auth, source_id, body)
    return ok(request, [c.model_dump(by_alias=True) for c in channels])
