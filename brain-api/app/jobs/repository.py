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
    sync_cursor: str | None = None


class JobsRepository:
    async def get_sync_state(self, session: AsyncSession, source_id: str) -> SyncState | None:
        row = (
            await session.execute(
                text(
                    """
                    SELECT id, provider, access_token_enc, refresh_token_enc,
                           token_expires_at, external_account_id, last_synced_at,
                           sync_cursor
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
        self,
        session: AsyncSession,
        source_id: str,
        synced_at: datetime | None,
        sync_cursor: str | None = None,
    ) -> None:
        # ``sync_cursor`` is the opaque per-connection cursor for token-cursor
        # providers (Drive pageToken / Gmail historyId); NULL leaves it untouched so
        # timestamp providers (Notion/GitHub) keep relying on last_synced_at.
        await session.execute(
            text(
                """
                UPDATE source_connections
                   SET last_synced_at = COALESCE(:synced_at, last_synced_at),
                       sync_cursor = COALESCE(:sync_cursor, sync_cursor),
                       sync_status = 'healthy',
                       status = 'connected',
                       updated_at = now()
                 WHERE id = :id
                """
            ).bindparams(id=source_id, synced_at=synced_at, sync_cursor=sync_cursor)
        )

    async def update_tokens(
        self,
        session: AsyncSession,
        source_id: str,
        *,
        access_token_enc: bytes,
        token_expires_at: datetime | None,
        refresh_token_enc: bytes | None = None,
    ) -> None:
        """Persist a refreshed access token (+ rotated refresh token, if any).

        Google access tokens expire hourly; persisting the refreshed token avoids a
        re-refresh every sweep and survives a rotated refresh token.
        """
        await session.execute(
            text(
                """
                UPDATE source_connections
                   SET access_token_enc = :access_token_enc,
                       token_expires_at = :token_expires_at,
                       refresh_token_enc = COALESCE(:refresh_token_enc, refresh_token_enc),
                       updated_at = now()
                 WHERE id = :id
                """
            ).bindparams(
                id=source_id,
                access_token_enc=access_token_enc,
                token_expires_at=token_expires_at,
                refresh_token_enc=refresh_token_enc,
            )
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

    # ── push-notification subscriptions (Google watch channels) ──────────────────
    async def insert_subscription(
        self,
        session: AsyncSession,
        *,
        sub_id: str,
        workspace_id: str,
        provider: str,
        source_ref_id: str,
        target_id: str,
        secret_enc: bytes | None,
        expires_at: datetime | None,
    ) -> None:
        await session.execute(
            text(
                """
                INSERT INTO webhook_subscriptions
                    (id, workspace_id, provider, source_ref_id, target_id,
                     secret_enc, expires_at, status)
                VALUES
                    (:id, :workspace_id, CAST(:provider AS source_provider),
                     :source_ref_id, :target_id, :secret_enc, :expires_at, 'active')
                """
            ).bindparams(
                id=sub_id,
                workspace_id=workspace_id,
                provider=provider,
                source_ref_id=source_ref_id,
                target_id=target_id,
                secret_enc=secret_enc,
                expires_at=expires_at,
            )
        )

    async def get_subscription_by_ref(
        self, session: AsyncSession, provider: str, source_ref_id: str
    ) -> tuple[str, str, bytes | None] | None:
        """Return ``(workspace_id, source_id, secret_enc)`` for a push channel ref."""
        row = (
            await session.execute(
                text(
                    """
                    SELECT workspace_id, target_id, secret_enc
                      FROM webhook_subscriptions
                     WHERE provider = CAST(:provider AS source_provider)
                       AND source_ref_id = :ref
                       AND status = 'active'
                    """
                ).bindparams(provider=provider, ref=source_ref_id)
            )
        ).first()
        return (row.workspace_id, row.target_id, row.secret_enc) if row else None

    async def list_expiring_subscriptions(
        self, session: AsyncSession, before: datetime
    ) -> list[tuple[str, str, str, str]]:
        """Return ``(sub_id, workspace_id, provider, source_id)`` for channels near expiry."""
        rows = (
            await session.execute(
                text(
                    """
                    SELECT id, workspace_id, provider, target_id
                      FROM webhook_subscriptions
                     WHERE status = 'active'
                       AND expires_at IS NOT NULL
                       AND expires_at < :before
                    """
                ).bindparams(before=before)
            )
        ).all()
        return [(r.id, r.workspace_id, r.provider, r.target_id) for r in rows]

    async def update_subscription(
        self,
        session: AsyncSession,
        sub_id: str,
        *,
        source_ref_id: str,
        secret_enc: bytes | None,
        expires_at: datetime | None,
    ) -> None:
        await session.execute(
            text(
                """
                UPDATE webhook_subscriptions
                   SET source_ref_id = :source_ref_id,
                       secret_enc = COALESCE(:secret_enc, secret_enc),
                       expires_at = :expires_at
                 WHERE id = :id
                """
            ).bindparams(
                id=sub_id,
                source_ref_id=source_ref_id,
                secret_enc=secret_enc,
                expires_at=expires_at,
            )
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
