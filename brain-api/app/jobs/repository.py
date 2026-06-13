"""Data access for background jobs (the only place job SQL lives).

Runs inside the job's ``run_in_tenant`` transaction, so RLS applies to workers
too. The idempotent event insert relies on the
``(workspace_id, provider, external_event_id)`` unique constraint on
``source_events`` — a replayed webhook or an overlapping sweep window is a no-op.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.base import RawEvent


@dataclass(frozen=True)
class SyncState:
    id: str
    provider: str
    access_token_enc: bytes | None
    refresh_token_enc: bytes | None
    token_expires_at: datetime | None
    external_account_id: str | None
    last_synced_at: datetime | None


class JobsRepository:
    async def get_sync_state(self, session: AsyncSession, source_id: str) -> SyncState | None:
        row = (
            await session.execute(
                text(
                    """
                    SELECT id, provider, access_token_enc, refresh_token_enc,
                           token_expires_at, external_account_id, last_synced_at
                      FROM source_connections
                     WHERE id = :id
                    """
                ).bindparams(id=source_id)
            )
        ).mappings().first()
        return SyncState(**row) if row else None

    async def insert_event(
        self, session: AsyncSession, workspace_id: str, event: RawEvent
    ) -> bool:
        """Insert one normalized event. Returns False if it was a duplicate."""
        result = await session.execute(
            text(
                """
                INSERT INTO source_events
                    (workspace_id, provider, event_type, source_id,
                     external_event_id, payload, outcome)
                VALUES
                    (:workspace_id, CAST(:provider AS source_provider), :event_type, :source_id,
                     :external_event_id, CAST(:payload AS jsonb), 'queued')
                ON CONFLICT ON CONSTRAINT source_events_workspace_provider_event_key
                DO NOTHING
                """
            ).bindparams(
                workspace_id=workspace_id,
                provider=event.provider,
                event_type=event.event_type,
                source_id=event.source_id,
                external_event_id=event.external_event_id,
                payload=json.dumps(event.raw),
            )
        )
        return (result.rowcount or 0) > 0

    async def advance_sync(
        self, session: AsyncSession, source_id: str, synced_at: datetime | None
    ) -> None:
        await session.execute(
            text(
                """
                UPDATE source_connections
                   SET last_synced_at = COALESCE(:synced_at, last_synced_at),
                       sync_status = 'healthy',
                       status = 'connected',
                       updated_at = now()
                 WHERE id = :id
                """
            ).bindparams(id=source_id, synced_at=synced_at)
        )

    async def mark_error(
        self, session: AsyncSession, source_id: str, *, auth_broken: bool
    ) -> None:
        # An auth failure flips the connection to 'error' (re-auth required); other
        # transient errors only mark the sync unhealthy so the next sweep retries.
        await session.execute(
            text(
                """
                UPDATE source_connections
                   SET sync_status = 'error',
                       status = CASE WHEN :auth_broken THEN 'error' ELSE status END,
                       updated_at = now()
                 WHERE id = :id
                """
            ).bindparams(id=source_id, auth_broken=auth_broken)
        )

    async def resolve_by_account(
        self, session: AsyncSession, provider: str, external_account_id: str
    ) -> tuple[str, str] | None:
        """Return ``(source_id, workspace_id)`` for a provider account (webhook routing)."""
        row = (
            await session.execute(
                text(
                    """
                    SELECT id, workspace_id FROM source_connections
                     WHERE provider = CAST(:provider AS source_provider)
                       AND external_account_id = :account
                    """
                ).bindparams(provider=provider, account=external_account_id)
            )
        ).first()
        return (row.id, row.workspace_id) if row else None
