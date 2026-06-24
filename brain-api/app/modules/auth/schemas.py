"""Auth request/response schemas (BACKEND_BEST_PRACTICES.md §3, §5).

Requests reject unknown keys (``extra="forbid"``); responses serialize to
camelCase per the API contract. The per-endpoint handlers that consume these
are implemented by the sibling endpoint tickets.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr
from pydantic.alias_generators import to_camel

# Response schemas share the camelCase serialization base used API-wide.
from app.shared.schemas import CamelModel as _Response


class _Request(BaseModel):
    # Accept camelCase request bodies (the API contract uses camelCase JSON
    # fields, e.g. ``refreshToken``) while still allowing the snake_case field
    # name; reject any unexpected key (mass-assignment defense).
    model_config = ConfigDict(
        extra="forbid",
        alias_generator=to_camel,
        populate_by_name=True,
    )


# ── requests ──────────────────────────────────────────────────────────────────
class EmailSignupRequest(_Request):
    email: EmailStr


class EmailSigninRequest(_Request):
    email: EmailStr


class OAuthCallbackRequest(_Request):
    code: str
    state: str


class RefreshRequest(_Request):
    refresh_token: str | None = None  # falls back to the httpOnly cookie


class LogoutRequest(_Request):
    refresh_token: str | None = None


# ── responses ─────────────────────────────────────────────────────────────────
class UserOut(_Response):
    id: str
    email: str
    name: str | None = None


class WorkspaceOut(_Response):
    id: str
    name: str
    slug: str


class AuthSessionOut(_Response):
    user: UserOut
    workspace: WorkspaceOut | None
    access_token: str
    refresh_token: str
    next_step: Literal["onboarding", "dashboard"]


class TokenPairOut(_Response):
    access_token: str
    refresh_token: str


class OAuthStartOut(_Response):
    authorization_url: str
    state: str
