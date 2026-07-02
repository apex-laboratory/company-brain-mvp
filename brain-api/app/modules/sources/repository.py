"""Source connection data access (the only place sources SQL lives).

Two access modes:
  * ``oauth_states`` rows are read/written on a **service-role** session — the
    callback resolves a state hash *before* any workspace context exists, and the
    table has no RLS (privileged lookup by hash). All other tables are RLS-guarded
    and are accessed inside ``run_in_tenant`` by the service.
  * ``source_connections`` / ``source_channels`` queries run inside the caller's
    tenant transaction; RLS (admin-only for connections) backstops them.

All queries are parameterized — never string-built.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class ResolvedState:
    id: str
    user_id: str
    workspace_id: str
    redirect_uri: str


class SourcesRepository:
    """Stateless repository; methods take the session they run in."""

    # ── oauth_states (service-role, no RLS) ──────────────────────────────────────
    async def create_oauth_state(
        self,
        session: AsyncSession,
        *,
        state_hash: bytes,
        provider: str,
        redirect_uri: str,
        user_id: str,
        workspace_id: str,
        expires_at: datetime,
    ) -> None:
        await session.execute(
            text(
                """
                INSERT INTO oauth_states
                    (user_id, workspace_id, provider, redirect_uri, state_hash, expires_at)
                VALUES (:user_id, :workspace_id, :provider, :redirect_uri, :state_hash, :expires_at)
                """
            ).bindparams(
                user_id=user_id,
                workspace_id=workspace_id,
                provider=provider,
                redirect_uri=redirect_uri,
                state_hash=state_hash,
                expires_at=expires_at,
            )
        )
        await session.commit()

    async def consume_oauth_state(
        self,
        session: AsyncSession,
        *,
        state_hash: bytes,
        provider: str,
        now: datetime,
    ) -> ResolvedState | None:
        """Atomically claim an unconsumed, unexpired state for ``provider``.

        The single ``UPDATE ... RETURNING`` makes consumption race-safe: a replayed
        state finds ``consumed_at`` already set and matches no row.
        """
        row = (
            await session.execute(
                text(
                    """
                    UPDATE oauth_states
                       SET consumed_at = :now
                     WHERE state_hash = :state_hash
                       AND provider = :provider
                       AND consumed_at IS NULL
                       AND expires_at > :now
                    RETURNING id, user_id, workspace_id, redirect_uri
                    """
                ).bindparams(state_hash=state_hash, provider=provider, now=now)
            )
        ).first()
        await session.commit()
        if row is None:
            return None
        return ResolvedState(
            id=row.id,
            user_id=row.user_id,
            workspace_id=row.workspace_id,
            redirect_uri=row.redirect_uri,
        )

    # ── source_connections (tenant, admin-only RLS) ──────────────────────────────
    async def upsert_connection(
        self,
        session: AsyncSession,
        *,
        connection_id: str,
        workspace_id: str,
        provider: str,
        name: str,
        access_token_enc: bytes,
        refresh_token_enc: bytes | None,
        token_expires_at: datetime | None,
        scopes: list[str],
        external_account_id: str | None,
        connected_by: str,
    ) -> str:
        """Insert or refresh a connection. Returns the connection id."""
        row = (
            await session.execute(
                text(
                    """
                    INSERT INTO source_connections
                        (id, workspace_id, provider, name, status, sync_status,
                         access_token_enc, refresh_token_enc, token_expires_at,
                         scopes, external_account_id, connected_by)
                    VALUES
                        (:id, :workspace_id, CAST(:provider AS source_provider), :name,
                         'connected', 'pending',
                         :access_token_enc, :refresh_token_enc, :token_expires_at,
                         :scopes, :external_account_id, :connected_by)
                    ON CONFLICT ON CONSTRAINT source_connections_workspace_id_provider_account_key
                    DO UPDATE SET
                         status = 'connected',
                         access_token_enc = EXCLUDED.access_token_enc,
                         refresh_token_enc = EXCLUDED.refresh_token_enc,
                         token_expires_at = EXCLUDED.token_expires_at,
                         scopes = EXCLUDED.scopes,
                         name = EXCLUDED.name,
                         updated_at = now()
                    RETURNING id
                    """
                ).bindparams(
                    id=connection_id,
                    workspace_id=workspace_id,
                    provider=provider,
                    name=name,
                    access_token_enc=access_token_enc,
                    refresh_token_enc=refresh_token_enc,
                    token_expires_at=token_expires_at,
                    scopes=scopes,
                    external_account_id=external_account_id,
                    connected_by=connected_by,
                )
            )
        ).first()
        return row.id  # type: ignore[union-attr]

    async def list_connections(self, session: AsyncSession) -> list[dict]:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT id, provider, name, status, sync_status,
                           external_account_id, last_synced_at, health, created_at
                      FROM source_connections
                     ORDER BY created_at DESC
                    """
                )
            )
        ).mappings().all()
        return [dict(r) for r in rows]

    async def get_connection_secrets(
        self, session: AsyncSession, connection_id: str
    ) -> dict | None:
        row = (
            await session.execute(
                text(
                    """
                    SELECT id, provider, access_token_enc, refresh_token_enc, token_expires_at
                      FROM source_connections
                     WHERE id = :id
                    """
                ).bindparams(id=connection_id)
            )
        ).mappings().first()
        return dict(row) if row else None

    async def delete_connection(self, session: AsyncSession, connection_id: str) -> bool:
        result = await session.execute(
            text("DELETE FROM source_connections WHERE id = :id").bindparams(id=connection_id)
        )
        return (result.rowcount or 0) > 0

    async def revoke_subscriptions_for_source(
        self, session: AsyncSession, source_id: str
    ) -> None:
        """Mark any push-channel subscriptions for this source revoked (stops renewal)."""
        await session.execute(
            text(
                "UPDATE webhook_subscriptions SET status = 'revoked' WHERE target_id = :sid"
            ).bindparams(sid=source_id)
        )

    # ── source_channels (tenant) ──────────────────────────────────────────────
    async def list_channels(self, session: AsyncSession, source_id: str) -> list[dict]:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT id, external_id, name, selected, item_count
                      FROM source_channels
                     WHERE source_id = :source_id
                     ORDER BY name
                    """
                ).bindparams(source_id=source_id)
            )
        ).mappings().all()
        return [dict(r) for r in rows]

    async def update_lookback(
        self, session: AsyncSession, source_id: str, lookback_days: int
    ) -> None:
        await session.execute(
            text(
                """
                UPDATE source_connections
                   SET lookback_days = :days, updated_at = now()
                 WHERE id = :id
                """
            ).bindparams(id=source_id, days=lookback_days)
        )

    async def upsert_channel(
        self,
        session: AsyncSession,
        *,
        channel_id: str,
        workspace_id: str,
        source_id: str,
        provider: str,
        external_id: str,
        name: str,
        selected: bool,
    ) -> None:
        await session.execute(
            text(
                """
                INSERT INTO source_channels
                    (id, workspace_id, source_id, provider, external_id, name, selected)
                VALUES
                    (:id, :workspace_id, :source_id, CAST(:provider AS source_provider),
                     :external_id, :name, :selected)
                ON CONFLICT ON CONSTRAINT source_channels_source_id_external_id_key
                DO UPDATE SET name = EXCLUDED.name, selected = EXCLUDED.selected
                """
            ).bindparams(
                id=channel_id,
                workspace_id=workspace_id,
                source_id=source_id,
                provider=provider,
                external_id=external_id,
                name=name,
                selected=selected,
            )
        )

    async def get_connection_provider(
        self, session: AsyncSession, connection_id: str
    ) -> str | None:
        row = (
            await session.execute(
                text(
                    "SELECT provider FROM source_connections WHERE id = :id"
                ).bindparams(id=connection_id)
            )
        ).first()
        return row.provider if row else None
