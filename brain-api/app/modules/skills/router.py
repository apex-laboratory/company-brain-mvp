"""Skills delivery + interactions routers (Phase 5 — PRD §14).

Two routers mounted under ``/api/v1``:

* ``/skills`` — semantic search, full body, version history, portable export.
* ``/interactions`` — the Feature 15a override feedback loop.

Reads are gated by ``require_brain_access`` (API keys need the ``brain:query``
scope; dashboard JWTs need role ≥ viewer). Export is admin-only — it dumps the
whole knowledge corpus, so it is intentionally not reachable with an API key.
Everything is RLS-scoped in the service; these gates are defense in depth.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response

from app.modules.skills.schemas import OverrideRequest
from app.modules.skills.service import SkillsService
from app.shared.http.respond import ok
from app.shared.middleware.authenticate import AuthContext, get_auth_context
from app.shared.middleware.authorize import require_brain_access, require_role
from app.shared.middleware.rate_limit import (
    BRAIN_LIMIT,
    EXPORT_LIMIT,
    limiter,
    workspace_key,
)

router = APIRouter(prefix="/skills", tags=["skills"])
interactions_router = APIRouter(prefix="/interactions", tags=["interactions"])

_service = SkillsService()


@router.get("/search", dependencies=[Depends(require_brain_access("brain:query"))])
@limiter.limit(BRAIN_LIMIT, key_func=workspace_key)
async def search_skills(
    request: Request,
    q: Annotated[str, Query(min_length=1, max_length=2000)],
    limit: Annotated[int, Query(ge=1, le=20)] = 5,
    auth: AuthContext = Depends(get_auth_context),
):
    """pgvector semantic search over published skills."""
    results = await _service.search(auth, q, limit)
    return ok(request, [r.model_dump(by_alias=True) for r in results])


@router.get("/export", dependencies=[Depends(require_role("admin"))])
@limiter.limit(EXPORT_LIMIT, key_func=workspace_key)
async def export_skills(
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
) -> Response:
    """Export all published skills as a portable markdown bundle (zip)."""
    data = await _service.export_bundle(auth)
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="company-brain-skills.zip"'},
    )


@router.get("/{skill_id}", dependencies=[Depends(require_brain_access("brain:query"))])
@limiter.limit(BRAIN_LIMIT, key_func=workspace_key)
async def get_skill(
    skill_id: str,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    """Full skill body."""
    skill = await _service.get(auth, skill_id)
    return ok(request, skill.model_dump(by_alias=True))


@router.get(
    "/{skill_id}/versions", dependencies=[Depends(require_brain_access("brain:query"))]
)
@limiter.limit(BRAIN_LIMIT, key_func=workspace_key)
async def get_skill_versions(
    skill_id: str,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    """Full version history for a skill."""
    versions = await _service.versions(auth, skill_id)
    return ok(request, [v.model_dump(by_alias=True) for v in versions])


@interactions_router.post(
    "/{interaction_id}/override",
    dependencies=[Depends(require_brain_access("skills:invoke"))],
)
@limiter.limit(BRAIN_LIMIT, key_func=workspace_key)
async def report_override(
    interaction_id: str,
    request: Request,
    body: OverrideRequest | None = None,
    auth: AuthContext = Depends(get_auth_context),
):
    """Report an agent override; drops confidence and may open a review."""
    result = await _service.override(auth, interaction_id, (body or OverrideRequest()).reason)
    return ok(request, result.model_dump(by_alias=True))
