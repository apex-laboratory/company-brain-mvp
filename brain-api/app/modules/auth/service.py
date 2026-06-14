"""Auth business logic (BACKEND_BEST_PRACTICES.md §2 layering, §7 tokens).

The service orchestrates the repository and owns token issuance. It is
framework-agnostic (no ``Request``/``Response``): the router passes the request
metadata it needs (``user_agent``, ``ip``) as plain values.

Covers passwordless email ``signup``/``signin`` (KAN-49) and refresh-token
rotation + ``logout`` (KAN-50). All four go through one token-minting path
(:meth:`_persist_session_tokens`), so access-token claims and refresh-token
storage stay consistent across login and rotation.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_session
from app.config.settings import settings
from app.modules.auth.repository import AuthRepository, MembershipRecord, UserRecord
from app.modules.auth.schemas import (
    AuthSessionOut,
    EmailSigninRequest,
    EmailSignupRequest,
    UserOut,
    WorkspaceOut,
)
from app.modules.auth.tokens import generate_refresh_token, mint_access_token
from app.shared.errors.app_error import ConflictError, NotFoundError, UnauthorizedError
from app.shared.helpers.crypto import sha256_hash
from app.shared.helpers.ids import generate_id
from app.shared.logger import get_logger

log = get_logger()


@dataclass(frozen=True)
class IssuedTokens:
    """A freshly minted access + refresh token pair (raw values)."""

    access_token: str
    refresh_token: str


class AuthService:
    def __init__(self, repository: AuthRepository | None = None) -> None:
        self._repository = repository or AuthRepository()

    # ── passwordless email auth (KAN-49) ──────────────────────────────────────
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

            try:
                user = await self._repository.create_user(
                    session, generate_id("user"), email
                )
            except IntegrityError as exc:
                # Lost a race to a concurrent signup: the pre-check passed but a
                # parallel request inserted this email first, tripping the
                # users_email_lower unique index. Map to the same 409 as the
                # pre-check rather than letting it surface as a 500.
                await session.rollback()
                raise ConflictError("Email already registered") from exc

            access_token, refresh_token = await self.issue_token_pair(
                session,
                user_id=user.id,
                workspace_id=None,
                role=None,
                user_agent=user_agent,
                ip=ip,
            )
            await self._repository.touch_last_login(session, user.id)
            await session.commit()

        return self._session_response(
            user,
            membership=None,
            access_token=access_token,
            refresh_token=refresh_token,
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

        return self._session_response(
            user,
            membership=membership,
            access_token=access_token,
            refresh_token=refresh_token,
        )

    @staticmethod
    def _session_response(
        user: UserRecord,
        *,
        membership: MembershipRecord | None,
        access_token: str,
        refresh_token: str,
    ) -> AuthSessionOut:
        """Assemble the session payload shared by signup and signin.

        ``next_step`` derives solely from workspace membership: a user with an
        active workspace lands on the ``dashboard``; one without (a fresh signup,
        or an account that never finished onboarding) goes to ``onboarding``.
        """
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

    # ── shared issuance ───────────────────────────────────────────────────────
    async def issue_token_pair(
        self,
        session: AsyncSession,
        *,
        user_id: str,
        workspace_id: str | None,
        role: str | None,
        user_agent: str | None,
        ip: str | None,
    ) -> tuple[str, str]:
        """Start a brand-new session in a fresh rotation family. Caller commits.

        Used by signup/signin (and OAuth/workspace-creation in sibling tickets).
        Refresh rotation reuses the same family via :meth:`refresh`.
        """
        family_id = generate_id("refresh_token")
        access_token, raw_refresh, _ = await self._persist_session_tokens(
            session,
            user_id=user_id,
            workspace_id=workspace_id,
            role=role,
            family_id=family_id,
            user_agent=user_agent,
            ip=ip,
        )
        return access_token, raw_refresh

    # ── refresh rotation (KAN-50) ──────────────────────────────────────────────
    async def refresh(
        self,
        session: AsyncSession,
        *,
        raw_token: str,
        user_agent: str | None = None,
        ip: str | None = None,
    ) -> IssuedTokens:
        """Rotate a refresh token, returning a new pair.

        Enforces, in order: existence, family reuse detection, expiry. On
        success the presented token is revoked and linked to its replacement.
        Every failure surfaces as a generic 401 so the response never reveals
        which check failed.
        """
        # Lock the row so concurrent rotations of the same token serialize.
        row = await self._repository.find_by_token_hash(
            session, sha256_hash(raw_token), for_update=True
        )

        if row is None:
            raise UnauthorizedError("Invalid refresh token")

        if row.revoked_at is not None:
            # A revoked token was replayed — treat the whole family as compromised.
            await self.revoke_family(session, row.family_id)
            await session.commit()
            log.warning(
                "refresh_token_reuse_detected",
                user_id=row.user_id,
                family_id=row.family_id,
            )
            raise UnauthorizedError(
                "Session invalidated due to token reuse. Please sign in again."
            )

        if row.expires_at < datetime.now(UTC):
            raise UnauthorizedError("Refresh token expired")

        # Claims reflect the user's *current* membership, so role changes take
        # effect on the next refresh. None before onboarding.
        membership = await self._repository.find_primary_membership(session, row.user_id)
        access_token, raw_refresh, new_token_id = await self._persist_session_tokens(
            session,
            user_id=row.user_id,
            workspace_id=membership.workspace_id if membership else None,
            role=membership.role if membership else None,
            family_id=row.family_id,
            user_agent=user_agent,
            ip=ip,
        )
        await self._repository.revoke_token(
            session, token_id=row.id, replaced_by=new_token_id
        )
        await session.commit()
        return IssuedTokens(access_token=access_token, refresh_token=raw_refresh)

    # ── logout (KAN-50) ────────────────────────────────────────────────────────
    async def logout(self, session: AsyncSession, *, raw_token: str) -> None:
        """Revoke the presented refresh token. Idempotent: an unknown or
        already-revoked token silently succeeds (no token-probing oracle)."""
        row = await self._repository.find_by_token_hash(session, sha256_hash(raw_token))
        if row is not None and row.revoked_at is None:
            await self._repository.revoke_token(session, token_id=row.id)
        await session.commit()

    async def revoke_family(self, session: AsyncSession, family_id: str) -> None:
        """Revoke every live token in a rotation family."""
        await self._repository.revoke_family(session, family_id)

    # ── internals ─────────────────────────────────────────────────────────────
    async def _persist_session_tokens(
        self,
        session: AsyncSession,
        *,
        user_id: str,
        workspace_id: str | None,
        role: str | None,
        family_id: str,
        user_agent: str | None,
        ip: str | None,
    ) -> tuple[str, str, str]:
        """Mint an access token and persist a new refresh-token row (hashed).

        Returns ``(access_token, raw_refresh_token, refresh_token_id)``. The id
        lets the rotation caller link the old row's ``replaced_by``. Does not
        commit or revoke anything.
        """
        access_token = mint_access_token(
            user_id=user_id, workspace_id=workspace_id, role=role
        )
        raw_refresh = generate_refresh_token()
        expires_at = datetime.now(UTC) + timedelta(
            seconds=settings.refresh_token_ttl_seconds
        )
        token_id = await self._repository.insert_refresh_token(
            session,
            user_id=user_id,
            token_hash=sha256_hash(raw_refresh),
            family_id=family_id,
            expires_at=expires_at,
            user_agent=user_agent,
            ip_address=ip,
        )
        return access_token, raw_refresh, token_id
