"""Brain chat router (BACKEND_ASKS §7, BRAIN_CHAT_RAG_PLAN).

Mounted under ``/api/v1`` as ``/brain``. Reads are gated by ``require_brain_access``
(API keys need the ``brain:query`` scope; dashboard JWTs need role ≥ viewer), so the
one surface serves both the browser dashboard and agents. Everything is RLS-scoped
in the service; these gates are defense in depth.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.modules.brain.service import BrainService
from app.shared.http.respond import ok
from app.shared.middleware.authenticate import AuthContext, get_auth_context
from app.shared.middleware.authorize import require_brain_access
from app.shared.middleware.rate_limit import BRAIN_LIMIT, limiter, workspace_key

router = APIRouter(prefix="/brain", tags=["brain"])

_service = BrainService()


@router.get("/status", dependencies=[Depends(require_brain_access("brain:query"))])
@limiter.limit(BRAIN_LIMIT, key_func=workspace_key)
async def brain_status(
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    """Whether the brain is ready to answer for this workspace (FE gate)."""
    status = await _service.status(auth)
    return ok(request, status.model_dump(by_alias=True))
