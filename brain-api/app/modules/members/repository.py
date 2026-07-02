"""Member data access — the only place this SQL lives (§2 layering).

Every query is workspace-scoped (``workspace_id = :workspace_id`` bound param).
RLS backstops each read/write: the ``members_select`` policy gates the roster to
workspace members, and ``invitations_admin`` gates invitation rows to admins.
All values are bound parameters — never string-built.

``count_pending_invites`` is deliberately a bare integer aggregate: invitation
*rows* carry the ``token_hash`` credential and are admin-only under RLS, but the
*count* is not sensitive. The service runs it on a privileged (service-role)
session for the all-members roster path so the seat figure is correct regardless
of the caller's role, while the roster rows themselves stay under tenant RLS.

Invitations store only a SHA-256 ``token_hash``; the raw invite token (used in
the acceptance email link) is never persisted.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# A pending invitation is one not yet accepted/revoked and not past its TTL.
# Centralized so the seat-count and the per-email duplicate check can never
# drift apart. Trusted constant (no interpolated user input) — safe to inline.
_PENDING_INVITE = "status = 'pending' AND expires_at > now()"


@dataclass(frozen=True)
class MemberRow:
    id: str  # the member's user id (usr_…)
    name: str | None
    email: str
    role: str
    title: str | None
    avatar_color: str | None


@dataclass(frozen=True)
class EmailStatus:
    is_member: bool
    has_pending_invite: bool


class MemberRepository:
    """Stateless repository; methods take the session they run in."""

    async def list_members(
        self, session: AsyncSession, workspace_id: str
    ) -> list[MemberRow]:
        """Active members of the workspace, admins first then by join time."""
        rows = (
            await session.execute(
                text(
                    """
                    SELECT u.id, u.name, u.email, m.role, u.title, u.avatar_color
                    FROM workspace_members m
                    JOIN users u ON u.id = m.user_id
                    WHERE m.workspace_id = :workspace_id AND m.is_active = TRUE
                    ORDER BY
                        CASE m.role
                            WHEN 'admin'  THEN 0
                            WHEN 'editor' THEN 1
                            ELSE 2
                        END,
                        m.joined_at ASC,
                        u.id ASC
                    """
                ).bindparams(workspace_id=workspace_id),
            )
        ).all()
        return [
            MemberRow(
                id=r.id,
                name=r.name,
                email=r.email,
                role=r.role,
                title=r.title,
                avatar_color=r.avatar_color,
            )
            for r in rows
        ]

    async def get_seat_limit(
        self, session: AsyncSession, workspace_id: str
    ) -> int | None:
        """The workspace's seat limit, or ``None`` if it isn't visible.

        Returns ``None`` when the workspace doesn't exist or is soft-deleted
        (the ``workspaces_select`` RLS policy filters ``deleted_at IS NOT NULL``),
        so the caller can map a stale-token request to 404 instead of crashing
        on ``int(None)``.
        """
        row = (
            await session.execute(
                text(
                    "SELECT seat_limit FROM workspaces WHERE id = :workspace_id"
                ).bindparams(workspace_id=workspace_id),
            )
        ).first()
        if row is None or row.seat_limit is None:
            return None
        return int(row.seat_limit)

    async def count_active_members(
        self, session: AsyncSession, workspace_id: str
    ) -> int:
        """Number of active members (each holds a seat)."""
        row = (
            await session.execute(
                text(
                    """
                    SELECT count(*) AS n FROM workspace_members
                    WHERE workspace_id = :workspace_id AND is_active = TRUE
                    """
                ).bindparams(workspace_id=workspace_id),
            )
        ).one()
        return int(row.n)

    async def count_pending_invites(
        self, session: AsyncSession, workspace_id: str
    ) -> int:
        """Number of outstanding pending invitations (each reserves a seat)."""
        row = (
            await session.execute(
                text(
                    f"""
                    SELECT count(*) AS n FROM invitations
                    WHERE workspace_id = :workspace_id AND {_PENDING_INVITE}
                    """
                ).bindparams(workspace_id=workspace_id),
            )
        ).one()
        return int(row.n)

    async def email_status(
        self, session: AsyncSession, workspace_id: str, email: str
    ) -> EmailStatus:
        """Whether ``email`` is already an active member or has a live invite.

        Matches case-insensitively (``lower(email)``); ``email`` is expected
        already normalized to lowercase by the schema validator.
        """
        row = (
            await session.execute(
                text(
                    f"""
                    SELECT
                        EXISTS (
                            SELECT 1
                            FROM workspace_members m
                            JOIN users u ON u.id = m.user_id
                            WHERE m.workspace_id = :workspace_id
                              AND m.is_active = TRUE
                              AND lower(u.email) = :email
                        ) AS is_member,
                        EXISTS (
                            SELECT 1
                            FROM invitations
                            WHERE workspace_id = :workspace_id
                              AND {_PENDING_INVITE}
                              AND lower(email) = :email
                        ) AS has_pending_invite
                    """
                ).bindparams(workspace_id=workspace_id, email=email),
            )
        ).one()
        return EmailStatus(
            is_member=bool(row.is_member),
            has_pending_invite=bool(row.has_pending_invite),
        )

    async def create_invitation(
        self,
        session: AsyncSession,
        *,
        invite_id: str,
        workspace_id: str,
        email: str,
        role: str,
        token_hash: bytes,
        invited_by: str,
    ) -> datetime:
        """Insert a pending invitation and return its ``created_at``.

        ``expires_at`` and ``status`` use the column defaults (pending, +7 days).
        Caller commits. Only the token hash is stored — never the raw token. A
        duplicate pending invite for the same workspace+email trips the
        ``invitations_workspace_email_pending_uq`` partial unique index
        (migration 0009) and raises ``IntegrityError``.
        """
        row = (
            await session.execute(
                text(
                    """
                    INSERT INTO invitations
                        (id, workspace_id, email, role, token_hash, invited_by)
                    VALUES
                        (:id, :workspace_id, :email,
                         CAST(:role AS member_role), :token_hash, :invited_by)
                    RETURNING created_at
                    """
                ).bindparams(
                    id=invite_id,
                    workspace_id=workspace_id,
                    email=email,
                    role=role,
                    token_hash=token_hash,
                    invited_by=invited_by,
                ),
            )
        ).one()
        return cast(datetime, row.created_at)
