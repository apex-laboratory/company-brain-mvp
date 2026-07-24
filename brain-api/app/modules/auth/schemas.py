"""Auth request/response schemas (BACKEND_BEST_PRACTICES.md §3, §5).

Requests reject unknown keys (``extra="forbid"``); responses serialize to
camelCase per the API contract. The per-endpoint handlers that consume these
are implemented by the sibling endpoint tickets.
"""
from __future__ import annotations

from typing import Literal

from pydantic import EmailStr

# Request/response schemas share the camelCase bases used API-wide.
from app.shared.schemas import CamelModel as _Response
from app.shared.schemas import CamelRequestModel as _Request


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


class MeOut(_Response):
    """Current authenticated user + workspace context (GET /auth/me).

    Lets the FE rehydrate session state on reload from the access token instead
    of trusting a localStorage snapshot. No tokens — the caller already holds a
    valid access token to reach this route."""

    user: UserOut
    workspace: WorkspaceOut | None
    role: str | None
    next_step: Literal["onboarding", "dashboard"]


class OAuthStartOut(_Response):
    authorization_url: str
    state: str
