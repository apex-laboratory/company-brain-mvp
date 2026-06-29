"""Member roster + invitation business logic (BEST_PRACTICES §2, §7, §8).

Framework-agnostic: the router passes the resolved ``AuthContext`` and the path
``workspace_id``; the service enforces membership, opens a tenant-scoped
transaction, and shapes the response. Listing the roster is available to any
workspace member (``members_select`` RLS); inviting is admin-only, applied as a
router dependency (``require_role("admin")``) and backstopped by the
``invitations_admin`` RLS policy.

Seat model: one seat is consumed by an active member **or** an outstanding
pending invitation (a seat is reserved when invited). Both the roster's
``usedSeats`` and the invite seat-limit check use this same definition, so the
list never advertises a seat the invite endpoint would refuse.

Invite tokens (§7): a random URL-safe token is generated so its SHA-256 hash can
be stored and emailed as an acceptance link. Only the hash is persisted; the raw
token is never returned or logged. The invite email is sent best-effort after the
row commits — a delivery failure is logged but does not roll back the invitation.
"""
from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass
from urllib.parse import quote

from sqlalchemy.exc import IntegrityError

from app.config.database import get_session
from app.config.settings import settings
from app.integrations import resend
from app.modules.members.repository import MemberRepository
from app.modules.members.schemas import (
    MemberInviteOut,
    MemberInviteRequest,
    MemberOut,
)
from app.shared.errors.app_error import ConflictError, NotFoundError
from app.shared.helpers.crypto import sha256_hash
from app.shared.helpers.ids import generate_id
from app.shared.logger import get_logger
from app.shared.middleware.authenticate import AuthContext
from app.shared.middleware.authorize import assert_workspace_member
from app.shared.middleware.with_tenant import tenant_session

log = get_logger()

# 32 random bytes → ~43-char URL-safe token. Only its hash is stored; this value
# is embedded in the invite-acceptance email link.
_INVITE_TOKEN_BYTES = 32


@dataclass(frozen=True)
class MemberRoster:
    """The roster plus the seat figures the list response exposes as meta.

    ``used_seats`` counts active members **and** pending invites; ``pending_invites``
    is broken out so a client can show member count (used − pending) and reserved
    seats separately.
    """

    members: list[MemberOut]
    seat_limit: int
    used_seats: int
    pending_invites: int


class MemberService:
    def __init__(self, repository: MemberRepository | None = None) -> None:
        self._repository = repository or MemberRepository()

    async def list_members(
        self, auth: AuthContext, workspace_id: str
    ) -> MemberRoster:
        workspace_id = assert_workspace_member(auth, workspace_id)

        async with tenant_session(auth, workspace_id) as session:
            rows, seat_limit = await asyncio.gather(
                self._repository.list_members(session, workspace_id),
                self._repository.get_seat_limit(session, workspace_id),
            )

        if seat_limit is None:
            # The token names a workspace that is gone or soft-deleted.
            raise NotFoundError("Workspace")

        # Pending invitations are admin-only under RLS, but the roster is visible
        # to every member — count them on a privileged (service-role) session so
        # the seat figure is correct regardless of the caller's role. Only the
        # integer count crosses the boundary; no invitation rows are exposed.
        async with get_session() as priv_session:
            pending_invites = await self._repository.count_pending_invites(
                priv_session, workspace_id
            )

        members = [
            MemberOut(
                id=row.id,
                name=row.name,
                email=row.email,
                role=row.role,  # DB member_role enum ⊆ MemberRole literal
                title=row.title,
                avatar_color=row.avatar_color,
                is_current_user=row.id == auth.user_id,
            )
            for row in rows
        ]
        return MemberRoster(
            members=members,
            seat_limit=seat_limit,
            used_seats=len(members) + pending_invites,
            pending_invites=pending_invites,
        )

    async def invite_member(
        self, auth: AuthContext, workspace_id: str, body: MemberInviteRequest
    ) -> MemberInviteOut:
        workspace_id = assert_workspace_member(auth, workspace_id)
        email = body.email  # already lowercased by the schema validator

        invite_id = generate_id("invite")
        raw_token = secrets.token_urlsafe(_INVITE_TOKEN_BYTES)

        # The caller is an admin (require_role), so invitations are readable under
        # the tenant RLS context here — no privileged session needed.
        async with tenant_session(auth, workspace_id) as session:
            status, seat_limit, active_members, pending_invites = await asyncio.gather(
                self._repository.email_status(session, workspace_id, email),
                self._repository.get_seat_limit(session, workspace_id),
                self._repository.count_active_members(session, workspace_id),
                self._repository.count_pending_invites(session, workspace_id),
            )

            if seat_limit is None:
                raise NotFoundError("Workspace")
            if status.is_member:
                raise ConflictError("This person is already a member.")
            if status.has_pending_invite:
                raise ConflictError("An invitation is already pending for this email.")
            # Active members and outstanding invites both hold a seat.
            if active_members + pending_invites >= seat_limit:
                raise ConflictError(
                    "Seat limit reached. Upgrade your plan or remove a member."
                )

            try:
                await self._repository.create_invitation(
                    session,
                    invite_id=invite_id,
                    workspace_id=workspace_id,
                    email=email,
                    role=body.role,
                    token_hash=sha256_hash(raw_token),
                    invited_by=auth.user_id,
                )
                await session.commit()
            except IntegrityError as exc:
                # Lost a race with a concurrent invite for the same email: the
                # pre-check passed but a parallel insert landed first, tripping the
                # invitations_workspace_email_pending_uq partial unique index
                # (migration 0009). Map to the same 409 as the pre-check.
                await session.rollback()
                raise ConflictError(
                    "An invitation is already pending for this email."
                ) from exc

        await self._send_invite_email(email=email, role=body.role, raw_token=raw_token)

        log.info(
            "member_invited",
            workspace_id=workspace_id,
            invite_id=invite_id,
            role=body.role,
        )
        return MemberInviteOut(
            invite_id=invite_id, email=email, role=body.role, status="pending"
        )

    @staticmethod
    async def _send_invite_email(*, email: str, role: str, raw_token: str) -> None:
        """Send the invite acceptance email, best-effort.

        Delivery failures are logged and swallowed so a transient Resend outage
        doesn't fail an invitation that already committed; the invite can be
        resent later. ``RESEND_API_KEY`` is all that's needed for this to work.
        """
        accept_url = f"{settings.app_base_url}/invite/accept?token={quote(raw_token)}"
        try:
            await resend.send_email(
                to=email,
                subject="You've been invited to Brainite",
                html=(
                    f"<p>You've been invited to join a Brainite workspace as "
                    f"<strong>{role}</strong>.</p>"
                    f'<p><a href="{accept_url}">Accept your invitation</a></p>'
                    f"<p>This link expires in 7 days.</p>"
                ),
                text=(
                    f"You've been invited to join a Brainite workspace as {role}.\n"
                    f"Accept your invitation: {accept_url}\n"
                    f"This link expires in 7 days."
                ),
            )
        except resend.EmailError:
            log.warning("invite_email_failed", role=role)
