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
