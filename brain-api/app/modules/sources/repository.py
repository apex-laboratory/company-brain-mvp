"""Source-integration data access — the only place this SQL lives (§2 layering).

Every query is workspace-scoped (``workspace_id = :workspace_id`` bound param)
and runs under the caller's tenant context. RLS backstops each operation: the
``connections_admin`` policy gates ``source_connections`` (it holds encrypted
tokens) to workspace admins, while ``channels_select`` lets any member read the
channel list and ``channels_write`` restricts changes to admins.

Provider tokens are stored only as AES-GCM ciphertext (``*_token_enc``); the raw
tokens are never persisted in plaintext and never selected back out here.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# time_range (API contract) -> lookback_days stored on the connection.
LOOKBACK_DAYS: dict[str, int] = {"30d": 30, "90d": 90, "6mo": 180, "all": 3650}


@dataclass(frozen=True)
class ConnectionRow:
    id: str
    provider: str
    name: str
    status: str
    sync_status: str
    last_synced_at: datetime | None
    health: int | None
    active_channel_count: int


@dataclass(frozen=True)
class ChannelRow:
    id: str
    name: str
    provider: str
    selected: bool
    item_count: int


class SourceRepository:
    """Stateless repository; methods take the session they run in."""

    async def list_connections(
        self, session: AsyncSession, workspace_id: str
    ) -> list[ConnectionRow]:
        """All non-disconnected source connections, oldest first."""
        rows = (
            await session.execute(
                text(
                    """
                    SELECT c.id, c.provider, c.name, c.status, c.sync_status,
                           c.last_synced_at, c.health,
                           (SELECT count(*) FROM source_channels ch
                             WHERE ch.source_id = c.id AND ch.selected) AS active_channel_count
                    FROM source_connections c
                    WHERE c.workspace_id = :workspace_id
                      AND c.status <> 'disconnected'
                    ORDER BY c.created_at ASC, c.id ASC
                    """
                ).bindparams(workspace_id=workspace_id),
            )
        ).all()
        return [_to_connection(r) for r in rows]

    async def upsert_connection(
        self,
        session: AsyncSession,
        *,
        new_id: str,
        workspace_id: str,
        provider: str,
        name: str,
        access_token_enc: bytes,
        refresh_token_enc: bytes | None,
        token_expires_at: datetime | None,
        scopes: Sequence[str],
        external_account_id: str,
        connected_by: str,
    ) -> ConnectionRow:
        """Connect or reconnect a provider account, atomically. Caller commits.

        One ``INSERT ... ON CONFLICT (workspace_id, provider, external_account_id)``
        so a reconnect updates the existing row (keeping its id) and a concurrent
        double-connect can't create duplicate rows. ``external_account_id`` must be
        non-NULL (the service substitutes a sentinel for providers that return no
        account id) — otherwise the unique constraint wouldn't fire on NULLs.
        ``active_channel_count`` is reported as 0 here; the list endpoint computes
        the live count.
        """
        row = (
            await session.execute(
                text(
                    """
                    INSERT INTO source_connections
                        (id, workspace_id, provider, name, status, sync_status,
                         access_token_enc, refresh_token_enc, token_expires_at,
                         scopes, external_account_id, connected_by)
                    VALUES
                        (:id, :workspace_id, CAST(:provider AS source_provider),
                         :name, 'connected', 'pending',
                         :access_token_enc, :refresh_token_enc, :token_expires_at,
                         :scopes, :external_account_id, :connected_by)
                    ON CONFLICT (workspace_id, provider, external_account_id)
                    DO UPDATE SET
                        status            = 'connected',
                        sync_status       = 'pending',
                        name              = EXCLUDED.name,
                        access_token_enc  = EXCLUDED.access_token_enc,
                        refresh_token_enc = EXCLUDED.refresh_token_enc,
                        token_expires_at  = EXCLUDED.token_expires_at,
                        scopes            = EXCLUDED.scopes,
                        connected_by      = EXCLUDED.connected_by,
                        updated_at        = now()
                    RETURNING id, provider, name, status, sync_status,
                              last_synced_at, health
                    """
                ).bindparams(
                    id=new_id,
                    workspace_id=workspace_id,
                    provider=provider,
                    name=name,
                    access_token_enc=access_token_enc,
                    refresh_token_enc=refresh_token_enc,
                    token_expires_at=token_expires_at,
                    scopes=list(scopes),
                    external_account_id=external_account_id,
                    connected_by=connected_by,
                ),
            )
        ).one()
        return _to_connection(row, active_channel_count=0)

    async def disconnect(
        self, session: AsyncSession, workspace_id: str, source_id: str
    ) -> bool:
        """Soft-disconnect a source and wipe its stored tokens. Caller commits.

        Returns ``False`` when no live connection matched (→ 404). Tokens are
        cleared so a disconnected row can never be used to call the provider; the
        row itself is kept (status='disconnected') for audit and for revoking the
        associated webhook subscriptions in a later step.
        """
        row = (
            await session.execute(
                text(
                    """
                    UPDATE source_connections
                    SET status            = 'disconnected',
                        access_token_enc  = NULL,
                        refresh_token_enc = NULL,
                        token_expires_at  = NULL,
                        updated_at        = now()
                    WHERE id = :source_id
                      AND workspace_id = :workspace_id
                      AND status <> 'disconnected'
                    RETURNING id
                    """
                ).bindparams(source_id=source_id, workspace_id=workspace_id),
            )
        ).first()
        return row is not None

    async def list_channels(
        self, session: AsyncSession, workspace_id: str, source_id: str
    ) -> list[ChannelRow]:
        """Channels/spaces/projects known for a connection, by name."""
        rows = (
            await session.execute(
                text(
                    """
                    SELECT id, name, provider, selected, item_count
                    FROM source_channels
                    WHERE workspace_id = :workspace_id AND source_id = :source_id
                    ORDER BY name ASC, id ASC
                    """
                ).bindparams(workspace_id=workspace_id, source_id=source_id),
            )
        ).all()
        return [
            ChannelRow(
                id=r.id,
                name=r.name,
                provider=r.provider,
                selected=r.selected,
                item_count=int(r.item_count),
            )
            for r in rows
        ]

    async def set_lookback(
        self, session: AsyncSession, workspace_id: str, lookback_days: int
    ) -> None:
        """Apply the chosen time window to every connected source. Caller commits."""
        await session.execute(
            text(
                """
                UPDATE source_connections
                SET lookback_days = :lookback_days, updated_at = now()
                WHERE workspace_id = :workspace_id AND status = 'connected'
                """
            ).bindparams(workspace_id=workspace_id, lookback_days=lookback_days),
        )

    async def set_channel_selection(
        self,
        session: AsyncSession,
        workspace_id: str,
        provider: str,
        external_ids: Sequence[str],
    ) -> None:
        """Select exactly ``external_ids`` for a provider; deselect the rest.

        Selection is **provider-wide**, matching the provider-keyed scope contract
        (API_DOCUMENTATION.md §Configure Source Scope): ``external_ids`` is the full
        set of selected channels for that provider across the workspace. If a
        workspace has more than one connection of the same provider (e.g. two Slack
        workspaces), they share one selection set — scope is not per-connection.

        Caller commits. A no-op until the sync job has populated ``source_channels``
        for the provider, so it is safe to call during onboarding before ingestion.
        """
        await session.execute(
            text(
                """
                UPDATE source_channels
                SET selected = (external_id = ANY(:external_ids))
                WHERE workspace_id = :workspace_id
                  AND provider = CAST(:provider AS source_provider)
                """
            ).bindparams(
                workspace_id=workspace_id,
                provider=provider,
                external_ids=list(external_ids),
            ),
        )

    async def selected_item_total(
        self, session: AsyncSession, workspace_id: str
    ) -> int:
        """Sum of item counts across selected channels (0 before any sync)."""
        row = (
            await session.execute(
                text(
                    """
                    SELECT COALESCE(SUM(item_count), 0) AS total
                    FROM source_channels
                    WHERE workspace_id = :workspace_id AND selected
                    """
                ).bindparams(workspace_id=workspace_id),
            )
        ).one()
        return int(row.total)


def _to_connection(r: object, *, active_channel_count: int | None = None) -> ConnectionRow:
    """Map a result Row to a ConnectionRow.

    When ``active_channel_count`` is given (insert/update RETURNING, which doesn't
    project the channel count) it is used directly; otherwise it is read off the
    row (the list query computes it).
    """
    row = cast(ConnectionRow, r)  # SQLAlchemy Row attr access; fields line up by name
    return ConnectionRow(
        id=row.id,
        provider=row.provider,
        name=row.name,
        status=row.status,
        sync_status=row.sync_status,
        last_synced_at=row.last_synced_at,
        health=row.health,
        active_channel_count=(
            active_channel_count
            if active_channel_count is not None
            else int(row.active_channel_count)
        ),
    )
