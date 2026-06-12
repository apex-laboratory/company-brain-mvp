"""Auth data access (the only place auth SQL lives).

Pre-tenant lookups (resolve API key by hash, resolve user by email, find the
caller's primary workspace) run on a service-role session *before* any workspace
context exists, so they read the global tables directly. ``users`` enforces
FORCE row-level security with no INSERT policy and a SELECT policy gated on
tenant context, so these reads/writes rely on the privileged service-role
session bypassing RLS — the same path the API-key resolver already uses.
``refresh_tokens`` has no RLS. All queries are parameterized — never string-built.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class ResolvedApiKey:
    id: str
    workspace_id: str
    created_by: str
    scopes: list[str]


@dataclass(frozen=True)
class UserRecord:
    """A row from the global ``users`` table, projected for auth responses."""

    id: str
    email: str
    name: str | None


@dataclass(frozen=True)
class MembershipRecord:
    """A user's active workspace membership, joined with workspace display data."""

    workspace_id: str
    role: str
    workspace_name: str
    workspace_slug: str


class AuthRepository:
    """Stateless repository; methods take the session they run in."""

    # ── user lookups / writes (pre-tenant) ──────────────────────────────────────
    async def find_user_by_email(
        self,
        session: AsyncSession,
        email: str,
    ) -> UserRecord | None:
        """Return the user matching ``email`` (case-insensitive), or ``None``.

        Matches the ``lower(email)`` unique index; ``email`` is expected already
        normalized to lowercase by the service.
        """
        row = (
            await session.execute(
                text(
                    "SELECT id, email, name FROM users WHERE lower(email) = :email"
                ).bindparams(email=email),
            )
        ).first()
        if row is None:
            return None
        return UserRecord(id=row.id, email=row.email, name=row.name)

    async def create_user(
        self,
        session: AsyncSession,
        user_id: str,
        email: str,
    ) -> UserRecord:
        """Insert a new user and return it. Caller commits the transaction."""
        row = (
            await session.execute(
                text(
                    """
                    INSERT INTO users (id, email)
                    VALUES (:id, :email)
                    RETURNING id, email, name
                    """
                ).bindparams(id=user_id, email=email),
            )
        ).one()
        return UserRecord(id=row.id, email=row.email, name=row.name)

    async def find_primary_membership(
        self,
        session: AsyncSession,
        user_id: str,
    ) -> MembershipRecord | None:
        """Return the user's primary active workspace membership, or ``None``.

        Primary = admin membership if the user has one, otherwise the earliest
        joined. Soft-deleted workspaces are excluded.
        """
        row = (
            await session.execute(
                text(
                    """
                    SELECT m.workspace_id,
                           m.role,
                           w.name AS workspace_name,
                           w.slug AS workspace_slug
                    FROM workspace_members m
                    JOIN workspaces w ON w.id = m.workspace_id
                    WHERE m.user_id = :user_id
                      AND m.is_active = TRUE
                      AND w.deleted_at IS NULL
                    ORDER BY (m.role = 'admin') DESC, m.joined_at ASC
                    LIMIT 1
                    """
                ).bindparams(user_id=user_id),
            )
        ).first()
        if row is None:
            return None
        return MembershipRecord(
            workspace_id=row.workspace_id,
            role=row.role,
            workspace_name=row.workspace_name,
            workspace_slug=row.workspace_slug,
        )

    async def touch_last_login(self, session: AsyncSession, user_id: str) -> None:
        """Stamp ``last_login_at`` (and ``updated_at``) to now. Caller commits."""
        await session.execute(
            text(
                "UPDATE users SET last_login_at = now(), updated_at = now() "
                "WHERE id = :id"
            ).bindparams(id=user_id),
        )

    # ── refresh tokens ──────────────────────────────────────────────────────────
    async def create_refresh_token(
        self,
        session: AsyncSession,
        *,
        user_id: str,
        token_hash: bytes,
        family_id: str,
        expires_at: datetime,
        user_agent: str | None,
        ip_address: str | None,
    ) -> None:
        """Persist a refresh token by its SHA-256 hash. Caller commits.

        The raw token is never stored — only ``token_hash``. ``family_id`` groups
        a rotation chain so a replayed (revoked) token can revoke the whole family.
        ``id`` is assigned by the DB (gen_random_uuid()).
        """
        await session.execute(
            text(
                """
                INSERT INTO refresh_tokens
                    (user_id, token_hash, family_id, expires_at, user_agent, ip_address)
                VALUES
                    (:user_id, :token_hash, :family_id, :expires_at, :user_agent, :ip_address)
                """
            ).bindparams(
                user_id=user_id,
                token_hash=token_hash,
                family_id=family_id,
                expires_at=expires_at,
                user_agent=user_agent,
                ip_address=ip_address,
            ),
        )

    async def resolve_api_key_by_hash(
        self,
        session: AsyncSession,
        key_hash: bytes,
    ) -> ResolvedApiKey | None:
        """Return the live API key matching ``key_hash``, or ``None``.

        Excludes revoked and expired keys. Bumps ``last_used_at`` on a hit. Runs
        pre-tenant on the global ``api_keys`` table (lookup happens before a
        workspace context exists).
        """
        now = datetime.now(UTC)
        row = (
            await session.execute(
                text(
                    """
                    SELECT id, workspace_id, created_by, scopes
                    FROM api_keys
                    WHERE key_hash = :key_hash
                      AND revoked_at IS NULL
                      AND (expires_at IS NULL OR expires_at > :now)
                    """
                ).bindparams(key_hash=key_hash, now=now),
            )
        ).first()

        if row is None:
            return None

        await session.execute(
            text("UPDATE api_keys SET last_used_at = :now WHERE id = :id").bindparams(
                now=now, id=row.id
            )
        )
        await session.commit()

        return ResolvedApiKey(
            id=row.id,
            workspace_id=row.workspace_id,
            created_by=row.created_by,
            scopes=list(row.scopes or []),
        )
