"""Auth business logic (BACKEND_BEST_PRACTICES.md §2 layering, §7 tokens).

The service orchestrates the repository and owns the token-issuance logic. It is
framework-agnostic (no ``Request``/``Response``): the router passes the request
metadata it needs (``user_agent``, ``ip``) as plain values.

Implements KAN-49: passwordless email ``signup`` and ``signin``. Both create a
user session — an access token (short-lived JWT) plus a refresh token (stored
hashed) — and compute the ``next_step`` the frontend redirects to.
"""
from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from jose import jwt

from app.config.database import get_session
from app.config.settings import settings
from app.modules.auth.repository import AuthRepository
from app.modules.auth.schemas import (
    AuthSessionOut,
    EmailSigninRequest,
    EmailSignupRequest,
    UserOut,
    WorkspaceOut,
)
from app.shared.errors.app_error import ConflictError, NotFoundError
from app.shared.helpers.crypto import sha256_hash
from app.shared.helpers.ids import generate_id


class AuthService:
    def __init__(self, repository: AuthRepository | None = None) -> None:
        self._repository = repository or AuthRepository()

    async def signup(
        self,
        request: EmailSignupRequest,
        *,
        user_agent: str | None,
        ip: str | None,
    ) -> AuthSessionOut:
        """Create a new user and an initial session (no workspace yet).

        Raises ``ConflictError`` (409) if the email is already registered.
        """
        email = request.email.lower()
        async with get_session() as session:
            if await self._repository.find_user_by_email(session, email) is not None:
                raise ConflictError("Email already registered")

            user = await self._repository.create_user(session, generate_id("user"), email)
            access_token, refresh_token = await self.issue_token_pair(
                session,
                user_id=user.id,
                workspace_id=None,
                role=None,
                user_agent=user_agent,
                ip=ip,
            )
            await session.commit()

        return AuthSessionOut(
            user=UserOut(id=user.id, email=user.email, name=user.name),
            workspace=None,
            access_token=access_token,
            refresh_token=refresh_token,
            next_step="onboarding",
        )

    async def signin(
        self,
        request: EmailSigninRequest,
        *,
        user_agent: str | None,
        ip: str | None,
    ) -> AuthSessionOut:
        """Return an existing user's session, scoped to their primary workspace.

        Raises ``NotFoundError`` (404) if no account exists for the email. The
        ``next_step`` is ``dashboard`` when the user has a workspace, otherwise
        ``onboarding`` (account exists but onboarding never finished).
        """
        email = request.email.lower()
        async with get_session() as session:
            user = await self._repository.find_user_by_email(session, email)
            if user is None:
                raise NotFoundError("Account")

            membership = await self._repository.find_primary_membership(session, user.id)
            access_token, refresh_token = await self.issue_token_pair(
                session,
                user_id=user.id,
                workspace_id=membership.workspace_id if membership else None,
                role=membership.role if membership else None,
                user_agent=user_agent,
                ip=ip,
            )
            await self._repository.touch_last_login(session, user.id)
            await session.commit()

        workspace = (
            WorkspaceOut(
                id=membership.workspace_id,
                name=membership.workspace_name,
                slug=membership.workspace_slug,
            )
            if membership is not None
            else None
        )
        return AuthSessionOut(
            user=UserOut(id=user.id, email=user.email, name=user.name),
            workspace=workspace,
            access_token=access_token,
            refresh_token=refresh_token,
            next_step="dashboard" if membership is not None else "onboarding",
        )

    async def issue_token_pair(
        self,
        session: Any,
        *,
        user_id: str,
        workspace_id: str | None,
        role: str | None,
        user_agent: str | None,
        ip: str | None,
    ) -> tuple[str, str]:
        """Mint an access + refresh token pair, persisting the refresh hash.

        The access token is a stateless HS256 JWT (15 min). The refresh token is
        a 48-byte URL-safe random string returned to the caller but stored only as
        a SHA-256 hash, so a DB read can never recover a usable credential.
        """
        now = datetime.now(UTC)
        access_token = self._encode_access_token(user_id, workspace_id, role, now)

        raw_refresh_token = secrets.token_urlsafe(48)
        await self._repository.create_refresh_token(
            session,
            user_id=user_id,
            token_hash=sha256_hash(raw_refresh_token),
            family_id=generate_id("refresh_token"),
            expires_at=now + timedelta(seconds=settings.refresh_token_ttl_seconds),
            user_agent=user_agent,
            ip_address=ip,
        )
        return access_token, raw_refresh_token

    @staticmethod
    def _encode_access_token(
        user_id: str,
        workspace_id: str | None,
        role: str | None,
        now: datetime,
    ) -> str:
        """Sign the dashboard access token (claims per BACKEND_BEST_PRACTICES §7).

        ``workspace_id``/``role`` are ``None`` for a freshly signed-up user who has
        no workspace yet; the keys are always present so the auth middleware's
        required-claim check passes.
        """
        expires_at = now + timedelta(seconds=settings.access_token_ttl_seconds)
        payload: dict[str, Any] = {
            "sub": user_id,
            "workspace_id": workspace_id,
            "role": role,
            "scopes": [],
            "iat": int(now.timestamp()),
            "exp": int(expires_at.timestamp()),
        }
        token: str = jwt.encode(payload, settings.jwt_access_secret, algorithm="HS256")
        return token
