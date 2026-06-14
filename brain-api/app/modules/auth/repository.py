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
class OAuthStateRow:
    """Columns of ``oauth_states`` needed by the callback validation flow."""

    id: str
    user_id: str | None
    workspace_id: str | None
    provider: str
    redirect_uri: str
    expires_at: datetime
    consumed_at: datetime | None


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
        joined; ties are broken by ``workspace_id`` so the choice is stable across
        sign-ins. Soft-deleted workspaces are excluded.
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
                    ORDER BY (m.role = 'admin') DESC, m.joined_at ASC, m.workspace_id ASC
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

    # ── oauth_states (pre-tenant; looked up before workspace context exists) ───
    async def create_oauth_state(
        self,
        session: AsyncSession,
        *,
        state_hash: bytes,
        provider: str,
        redirect_uri: str,
        expires_at: datetime,
        user_id: str | None,
        workspace_id: str | None,
    ) -> None:
        """Persist a new oauth_states row. Caller commits."""
        await session.execute(
            text(
                """
                INSERT INTO oauth_states
                    (user_id, workspace_id, provider, redirect_uri,
                     state_hash, expires_at)
                VALUES
                    (:user_id, :workspace_id, :provider, :redirect_uri,
                     :state_hash, :expires_at)
                """
            ).bindparams(
                user_id=user_id,
                workspace_id=workspace_id,
                provider=provider,
                redirect_uri=redirect_uri,
                state_hash=state_hash,
                expires_at=expires_at,
            ),
        )

    async def find_oauth_state(
        self,
        session: AsyncSession,
        state_hash: bytes,
        *,
        for_update: bool = False,
    ) -> OAuthStateRow | None:
        """Look up an oauth_states row by its SHA-256 hash.

        ``for_update=True`` serialises concurrent callback requests for the same
        state so only the first can set ``consumed_at`` (single-use enforcement).
        """
        sql = """
            SELECT id, user_id, workspace_id, provider, redirect_uri,
                   expires_at, consumed_at
            FROM oauth_states
            WHERE state_hash = :state_hash
        """
        if for_update:
            sql += " FOR UPDATE"
        row = (
            await session.execute(
                text(sql).bindparams(state_hash=state_hash),
            )
        ).first()
        if row is None:
            return None
        return OAuthStateRow(
            id=str(row.id),
            user_id=row.user_id,
            workspace_id=row.workspace_id,
            provider=row.provider,
            redirect_uri=row.redirect_uri,
            expires_at=row.expires_at,
            consumed_at=row.consumed_at,
        )

    async def mark_oauth_state_consumed(
        self,
        session: AsyncSession,
        state_id: str,
    ) -> None:
        """Stamp ``consumed_at`` to enforce single-use. Caller commits."""
        await session.execute(
            text(
                """
                UPDATE oauth_states
                SET consumed_at = now()
                WHERE id = CAST(:id AS uuid)
                  AND consumed_at IS NULL
                """
            ).bindparams(id=state_id),
        )

    async def upsert_oauth_user(
        self,
        session: AsyncSession,
        *,
        user_id: str,
        email: str,
        name: str | None,
    ) -> UserRecord:
        """Find or create a user by email, back-filling name only when missing.

        Uses ``ON CONFLICT ((lower(email)))`` against the functional unique index
        created in migration 0003. If the email already exists the row is
        returned as-is, with ``name`` set only when the current value is NULL.
        """
        row = (
            await session.execute(
                text(
                    """
                    INSERT INTO users (id, email, name)
                    VALUES (:id, :email, :name)
                    ON CONFLICT ((lower(email))) DO UPDATE
                        SET name       = COALESCE(users.name, EXCLUDED.name),
                            updated_at = now()
                    RETURNING id, email, name
                    """
                ).bindparams(id=user_id, email=email, name=name),
            )
        ).one()
        return UserRecord(id=row.id, email=row.email, name=row.name)
