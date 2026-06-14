"""Auth data access (the only place auth SQL lives).

Pre-tenant lookups (resolve API key by hash, resolve user by email) run on a
service-role session *before* any workspace context exists, so they use the
global tables directly. All queries are parameterized — never string-built.

Per-endpoint queries (signup, refresh rotation, OAuth state) are added by the
sibling endpoint tickets; this module holds the shared primitives the auth
plumbing needs today.
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
class RefreshTokenRow:
    """The columns of ``refresh_tokens`` the rotation flow reasons about."""

    id: str
    user_id: str
    family_id: str
    expires_at: datetime
    revoked_at: datetime | None


@dataclass(frozen=True)
class Membership:
    """A user's active workspace membership, used to mint access-token claims."""

    workspace_id: str
    role: str


class AuthRepository:
    """Stateless repository; methods take the session they run in."""

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

    # ── refresh-token rotation (pre-tenant; refresh_tokens is a global table) ──
    async def find_by_token_hash(
        self,
        session: AsyncSession,
        token_hash: bytes,
        *,
        for_update: bool = False,
    ) -> RefreshTokenRow | None:
        """Look up a refresh token by its sha256 hash.

        The match is a parameterized ``token_hash = :h`` comparison in Postgres,
        so it is constant-time at the column level — the raw token is never
        compared in Python.

        ``for_update=True`` takes a row lock for the duration of the caller's
        transaction so concurrent rotations of the same token serialize: the
        first wins, the rest observe it revoked (reuse) instead of racing into a
        forked family with two live tokens.
        """
        sql = """
            SELECT id, user_id, family_id, expires_at, revoked_at
            FROM refresh_tokens
            WHERE token_hash = :token_hash
        """
        if for_update:
            sql += " FOR UPDATE"
        row = (
            await session.execute(
                text(sql).bindparams(token_hash=token_hash),
            )
        ).first()
        if row is None:
            return None
        return RefreshTokenRow(
            id=str(row.id),
            user_id=row.user_id,
            family_id=row.family_id,
            expires_at=row.expires_at,
            revoked_at=row.revoked_at,
        )

    async def insert_refresh_token(
        self,
        session: AsyncSession,
        *,
        user_id: str,
        token_hash: bytes,
        family_id: str,
        expires_at: datetime,
        user_agent: str | None,
        ip_address: str | None,
    ) -> str:
        """Persist a new refresh-token row; the DB assigns the UUID id."""
        row = (
            await session.execute(
                text(
                    """
                    INSERT INTO refresh_tokens
                        (user_id, token_hash, family_id, expires_at, user_agent, ip_address)
                    VALUES
                        (:user_id, :token_hash, :family_id, :expires_at,
                         :user_agent, CAST(:ip_address AS inet))
                    RETURNING id
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
        ).first()
        assert row is not None  # RETURNING always yields a row on INSERT
        return str(row.id)

    async def revoke_token(
        self,
        session: AsyncSession,
        *,
        token_id: str,
        replaced_by: str | None = None,
    ) -> None:
        """Revoke a single token by id (idempotent — only affects live rows).

        ``replaced_by`` links the rotation chain on a successful refresh; it is
        left ``NULL`` for logout.
        """
        await session.execute(
            text(
                """
                UPDATE refresh_tokens
                SET revoked_at = now(),
                    replaced_by = CAST(:replaced_by AS uuid)
                WHERE id = CAST(:token_id AS uuid)
                  AND revoked_at IS NULL
                """
            ).bindparams(token_id=token_id, replaced_by=replaced_by),
        )

    async def revoke_family(self, session: AsyncSession, family_id: str) -> None:
        """Revoke every still-live token in a rotation family (theft response)."""
        await session.execute(
            text(
                """
                UPDATE refresh_tokens
                SET revoked_at = now()
                WHERE family_id = :family_id
                  AND revoked_at IS NULL
                """
            ).bindparams(family_id=family_id),
        )

    async def find_active_membership(
        self,
        session: AsyncSession,
        user_id: str,
    ) -> Membership | None:
        """Return the user's active membership for access-token claims.

        Prefers an ``admin`` workspace, then the earliest-joined one. Returns
        ``None`` for a user who has not completed onboarding yet (no workspace).

        ``workspace_members`` has FORCE row-level security, so this pre-tenant
        read sets the ``app.current_user_id`` GUC (transaction-local) first: the
        ``members_self_select`` policy (migration 0009) lets a user read only
        their own membership rows. This makes the lookup correct whether or not
        the backend's connection bypasses RLS — it does not depend on a
        privileged role (BACKEND_BEST_PRACTICES.md §8).
        """
        await session.execute(
            text(
                "SELECT set_config('app.current_user_id', :user_id, true)"
            ).bindparams(user_id=user_id),
        )
        row = (
            await session.execute(
                text(
                    """
                    SELECT workspace_id, role
                    FROM workspace_members
                    WHERE user_id = :user_id
                      AND is_active = TRUE
                    ORDER BY (role = 'admin') DESC, joined_at ASC
                    LIMIT 1
                    """
                ).bindparams(user_id=user_id),
            )
        ).first()
        if row is None:
            return None
        return Membership(workspace_id=row.workspace_id, role=str(row.role))
