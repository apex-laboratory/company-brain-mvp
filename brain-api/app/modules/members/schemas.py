"""Member request/response schemas (BACKEND_BEST_PRACTICES.md §3, §5).

Responses serialize to camelCase per the API contract (API_DOCUMENTATION.md
§Members API). The invite request rejects unknown keys and normalizes the email
to lowercase at the edge so duplicate-member detection is case-insensitive.
"""
from __future__ import annotations

from typing import Literal

from pydantic import EmailStr, field_validator

from app.shared.schemas import CamelModel as _Response
from app.shared.schemas import CamelRequestModel as _Request

# The member-role hierarchy (API_DOCUMENTATION.md §Members API). Mirrors the
# ``member_role`` Postgres enum and the ranks in ``authorize.require_role``.
MemberRole = Literal["admin", "editor", "viewer"]


# ── requests ──────────────────────────────────────────────────────────────────
class MemberInviteRequest(_Request):
    email: EmailStr
    role: MemberRole = "viewer"

    @field_validator("email", mode="after")
    @classmethod
    def _normalize_email(cls, v: str) -> str:
        """Lowercase so membership/invite uniqueness is case-insensitive."""
        return v.lower()


# ── responses ─────────────────────────────────────────────────────────────────
class MemberOut(_Response):
    """A single roster entry. ``id`` is the member's user id (``usr_…``)."""

    id: str
    name: str | None
    email: str
    role: MemberRole
    title: str | None
    avatar_color: str | None
    is_current_user: bool


class MemberInviteOut(_Response):
    """Invite-creation response. The raw invite token is never returned."""

    invite_id: str
    email: str
    role: MemberRole
    status: Literal["pending"]
