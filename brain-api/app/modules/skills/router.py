"""Skills delivery + interactions routers (Phase 5 — PRD §14).

Two routers mounted under ``/api/v1``:

* ``/skills`` — semantic search, full body, version history, portable export, and
  the draft → review-queue submit (admin, like manual authoring).
* ``/interactions`` — the Feature 15a override feedback loop.

Reads are gated by ``require_brain_access`` (API keys need the ``brain:query``
scope; dashboard JWTs need role ≥ viewer). Export is admin-only — it dumps the
whole knowledge corpus, so it is intentionally not reachable with an API key.
Everything is RLS-scoped in the service; these gates are defense in depth.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response

from app.modules.skills.schemas import (
    CreateSkillRequest,
    OverrideRequest,
    SubmitForReviewRequest,
    UpdateSkillRequest,
)
from app.modules.skills.service import SkillsService
from app.shared.http.respond import created, ok
from app.shared.middleware.authenticate import AuthContext, get_auth_context
from app.shared.middleware.authorize import require_brain_access, require_role
from app.shared.middleware.rate_limit import (
    BRAIN_LIMIT,
    DASHBOARD_LIMIT,
    EXPORT_LIMIT,
    limiter,
    user_key,
    workspace_key,
)

router = APIRouter(prefix="/skills", tags=["skills"])
interactions_router = APIRouter(prefix="/interactions", tags=["interactions"])

_service = SkillsService()


_Status = Annotated[str | None, Query(pattern="^(stable|active|draft|review)$")]


@router.get("", dependencies=[Depends(require_brain_access("brain:query"))])
@limiter.limit(BRAIN_LIMIT, key_func=workspace_key)
async def list_skills(
    request: Request,
    status: _Status = None,
    source: Annotated[str | None, Query(max_length=40)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
    auth: AuthContext = Depends(get_auth_context),
):
    """Browse the registry without a query — paginated, filterable by status/source."""
    items, next_cursor = await _service.list(
        auth, status=status, source=source, limit=limit, cursor=cursor
    )
    return ok(
        request,
        [i.model_dump(by_alias=True) for i in items],
        nextCursor=next_cursor,
    )


@router.get("/stats", dependencies=[Depends(require_brain_access("brain:query"))])
@limiter.limit(BRAIN_LIMIT, key_func=workspace_key)
async def skill_stats(request: Request, auth: AuthContext = Depends(get_auth_context)):
    """Registry summary strip: total / stable / in-review / draft / calls·30d."""
    stats = await _service.stats(auth)
    return ok(request, stats.model_dump(by_alias=True))


@router.post("", dependencies=[Depends(require_role("admin"))])
@limiter.limit(DASHBOARD_LIMIT, key_func=user_key)
async def create_skill(
    request: Request,
    body: CreateSkillRequest,
    auth: AuthContext = Depends(get_auth_context),
):
    """Manually author a skill (admin): lands a draft in the review queue."""
    skill = await _service.create(auth, body)
    return created(request, skill.model_dump(by_alias=True))


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


@router.patch("/{skill_id}", dependencies=[Depends(require_role("editor"))])
@limiter.limit(DASHBOARD_LIMIT, key_func=user_key)
async def update_skill(
    skill_id: str,
    request: Request,
    body: UpdateSkillRequest,
    auth: AuthContext = Depends(get_auth_context),
):
    """Edit a skill (editor or admin): partial update of name/trigger/logic/description."""
    skill = await _service.update(auth, skill_id, body)
    return ok(request, skill.model_dump(by_alias=True))


@router.delete("/{skill_id}", dependencies=[Depends(require_role("admin"))])
@limiter.limit(DASHBOARD_LIMIT, key_func=user_key)
async def delete_skill(
    skill_id: str,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    """Soft-delete a skill (admin only)."""
    await _service.delete(auth, skill_id)
    return ok(request, {"id": skill_id, "deleted": True})


@router.post("/{skill_id}/submit", dependencies=[Depends(require_role("admin"))])
@limiter.limit(DASHBOARD_LIMIT, key_func=user_key)
async def submit_skill_for_review(
    skill_id: str,
    request: Request,
    body: SubmitForReviewRequest | None = None,
    auth: AuthContext = Depends(get_auth_context),
):
    """Send a draft skill to the review queue (draft → review, opens a card)."""
    result = await _service.submit_for_review(
        auth, skill_id, (body or SubmitForReviewRequest()).note
    )
    return ok(request, result.model_dump(by_alias=True))


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
