"""Auth business logic (BACKEND_BEST_PRACTICES.md §2 layering).

The service orchestrates the repository and integrations. It is framework
agnostic: it takes the session plus plain request metadata (user agent, IP), not
a FastAPI ``Request``. This module owns refresh-token rotation and logout
(KAN-50); the signup/signin/OAuth methods are added by the sibling tickets and
reuse :meth:`AuthService.issue_token_pair`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.modules.auth.repository import AuthRepository
from app.modules.auth.tokens import generate_refresh_token, mint_access_token
from app.shared.errors.app_error import UnauthorizedError
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

    # ── shared issuance ───────────────────────────────────────────────────────
    async def issue_token_pair(
        self,
        session: AsyncSession,
        *,
        user_id: str,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> IssuedTokens:
        """Start a brand-new session: mint an access token and a refresh token
        in a fresh rotation family.

        Used by signup/signin/OAuth (sibling tickets) and after workspace
        creation. Refresh rotation reuses the same family via :meth:`refresh`.
        """
        family_id = generate_id("refresh_token")
        issued, _ = await self._mint_pair(
            session,
            user_id=user_id,
            family_id=family_id,
            user_agent=user_agent,
            ip_address=ip_address,
            commit=True,
        )
        return issued

    # ── refresh rotation ──────────────────────────────────────────────────────
    async def refresh(
        self,
        session: AsyncSession,
        *,
        raw_token: str,
        user_agent: str | None = None,
        ip_address: str | None = None,
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

        # Rotate within the same family, then revoke + link the old row.
        issued, new_token_id = await self._mint_pair(
            session,
            user_id=row.user_id,
            family_id=row.family_id,
            user_agent=user_agent,
            ip_address=ip_address,
            commit=False,
        )
        await self._repository.revoke_token(
            session, token_id=row.id, replaced_by=new_token_id
        )
        await session.commit()
        return issued

    # ── logout ────────────────────────────────────────────────────────────────
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
    async def _mint_pair(
        self,
        session: AsyncSession,
        *,
        user_id: str,
        family_id: str,
        user_agent: str | None,
        ip_address: str | None,
        commit: bool,
    ) -> tuple[IssuedTokens, str]:
        """Mint an access token and persist a new refresh-token row.

        Returns the token pair and the new row's id (so the caller can link the
        rotation chain). Does not revoke anything.
        """
        # Access-token claims reflect the user's *current* membership, so role
        # changes take effect on the next refresh. None before onboarding.
        membership = await self._repository.find_active_membership(session, user_id)
        workspace_id = membership.workspace_id if membership else None
        role = membership.role if membership else None
        access_token = mint_access_token(
            user_id=user_id,
            workspace_id=workspace_id,
            role=role,
        )

        raw_refresh = generate_refresh_token()
        expires_at = datetime.now(UTC) + timedelta(
            seconds=settings.refresh_token_ttl_seconds
        )
        new_token_id = await self._repository.insert_refresh_token(
            session,
            user_id=user_id,
            token_hash=sha256_hash(raw_refresh),
            family_id=family_id,
            expires_at=expires_at,
            user_agent=user_agent,
            ip_address=ip_address,
        )
        if commit:
            await session.commit()
        return IssuedTokens(access_token=access_token, refresh_token=raw_refresh), new_token_id
