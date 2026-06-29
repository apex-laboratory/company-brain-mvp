"""Workspace members router (API_DOCUMENTATION.md §Members API).

Mounted under ``/api/v1/workspaces`` by ``main.py``:

* ``GET  /{workspace_id}/members``        — list the roster + seat usage meta.
* ``POST /{workspace_id}/members/invite`` — invite a member by email (201).

Listing is available to any workspace member (the ``members_select`` RLS policy
gates the rows). Inviting requires the ``admin`` role (``require_role("admin")``),
also backstopped by the ``invitations_admin`` RLS policy. Both routes use the
dashboard surface limit (300/min per user).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.modules.members.schemas import MemberInviteRequest
from app.modules.members.service import MemberService
from app.shared.http.respond import created, ok
from app.shared.middleware.authenticate import AuthContext, get_auth_context
from app.shared.middleware.authorize import require_role
from app.shared.middleware.rate_limit import DASHBOARD_LIMIT, limiter, user_key

router = APIRouter(prefix="/workspaces", tags=["members"])


def get_member_service() -> MemberService:
    """Provider so the service can be overridden in tests."""
    return MemberService()


@router.get("/{workspace_id}/members")
@limiter.limit(DASHBOARD_LIMIT, key_func=user_key)
async def list_members(
    request: Request,
    workspace_id: str,
    auth: AuthContext = Depends(get_auth_context),
    service: MemberService = Depends(get_member_service),
) -> JSONResponse:
    roster = await service.list_members(auth, workspace_id)
    return ok(
        request,
        [member.model_dump(by_alias=True) for member in roster.members],
        seatLimit=roster.seat_limit,
        usedSeats=roster.used_seats,
        pendingInvites=roster.pending_invites,
    )


@router.post("/{workspace_id}/members/invite", status_code=201)
@limiter.limit(DASHBOARD_LIMIT, key_func=user_key)
async def invite_member(
    request: Request,
    workspace_id: str,
    body: MemberInviteRequest,
    auth: AuthContext = Depends(get_auth_context),
    _: None = Depends(require_role("admin")),
    service: MemberService = Depends(get_member_service),
) -> JSONResponse:
    invite = await service.invite_member(auth, workspace_id, body)
    return created(request, invite.model_dump(by_alias=True))
