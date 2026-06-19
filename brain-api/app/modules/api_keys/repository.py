"""API key data access — the only place this SQL lives (§2 layering).

Every query is workspace-scoped (``workspace_id = :workspace_id`` bound param)
and runs under the caller's admin tenant context (the ``api_keys_admin`` RLS
policy is the backstop). All values are bound parameters. The stored ``key_hash``
is a SHA-256 digest of the raw key; the raw key is never persisted.

Revocation is a soft delete (``revoked_at = now()``): the row is retained for
audit, excluded from the list, and rejected by the authenticator
(``revoked_at IS NULL`` filter), so it behaves as an immediate deletion to the
client while preserving history.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class ApiKeyRow:
    id: str
    name: str
    prefix: str
    scopes: list[str]
    created_at: datetime
    last_used_at: datetime | None


class ApiKeyRepository:
    """Stateless repository; methods take the session they run in."""

    async def list_keys(
        self, session: AsyncSession, workspace_id: str
    ) -> list[ApiKeyRow]:
        """All live (non-revoked) keys for the workspace, newest first."""
        rows = (
            await session.execute(
                text(
                    """
                    SELECT id, name, key_prefix, scopes, created_at, last_used_at
                    FROM api_keys
                    WHERE workspace_id = :workspace_id AND revoked_at IS NULL
                    ORDER BY created_at DESC, id DESC
                    """
                ).bindparams(workspace_id=workspace_id),
            )
        ).all()
        return [
            ApiKeyRow(
                id=r.id,
                name=r.name,
                prefix=r.key_prefix,
                scopes=list(r.scopes or []),
                created_at=r.created_at,
                last_used_at=r.last_used_at,
            )
            for r in rows
        ]

    async def create_key(
        self,
        session: AsyncSession,
        *,
        key_id: str,
        workspace_id: str,
        name: str,
        key_hash: bytes,
        key_prefix: str,
        scopes: Sequence[str],
        created_by: str,
    ) -> datetime:
        """Insert a new key row and return its ``created_at``. Caller commits."""
        row = (
            await session.execute(
                text(
                    """
                    INSERT INTO api_keys
                        (id, workspace_id, name, key_hash, key_prefix, scopes, created_by)
                    VALUES
                        (:id, :workspace_id, :name, :key_hash, :key_prefix,
                         :scopes, :created_by)
                    RETURNING created_at
                    """
                ).bindparams(
                    id=key_id,
                    workspace_id=workspace_id,
                    name=name,
                    key_hash=key_hash,
                    key_prefix=key_prefix,
                    scopes=list(scopes),
                    created_by=created_by,
                ),
            )
        ).one()
        return cast(datetime, row.created_at)

    async def revoke_key(
        self, session: AsyncSession, workspace_id: str, key_id: str
    ) -> bool:
        """Soft-delete a key. Returns ``False`` when no live key matched (→ 404).

        Idempotent at the SQL level (``revoked_at IS NULL`` guard); the boolean
        lets the service distinguish "revoked just now" from "no such live key".
        ``RETURNING id`` makes the "did a live row match" check explicit.
        """
        row = (
            await session.execute(
                text(
                    """
                    UPDATE api_keys
                    SET revoked_at = now()
                    WHERE id = :key_id
                      AND workspace_id = :workspace_id
                      AND revoked_at IS NULL
                    RETURNING id
                    """
                ).bindparams(key_id=key_id, workspace_id=workspace_id),
            )
        ).first()
        return row is not None
