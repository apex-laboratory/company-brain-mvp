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
    lookback_days: int = 90


class JobsRepository:
    async def get_sync_state(self, session: AsyncSession, source_id: str) -> SyncState | None:
        row = (
            await session.execute(
                text(
                    """
                    SELECT id, provider, access_token_enc, refresh_token_enc,
                           token_expires_at, external_account_id, last_synced_at,
                           sync_cursor, lookback_days
                      FROM source_connections
                     WHERE id = :id
                    """
                ).bindparams(id=source_id)
            )
        ).mappings().first()
        return SyncState(**row) if row else None

    async def insert_event(
        self,
        session: AsyncSession,
        workspace_id: str,
        event: RawEvent,
        sweep_id: str | None = None,
        *,
        source_connection_id: str | None = None,
    ) -> str | None:
        """Insert one normalized event. Returns the new event id, or ``None`` for a
        duplicate. The id is what the caller enqueues ``extract_event`` with.

        ``source_connection_id`` records which connection produced the event so the
        pipeline's expander resolves that connection's token (not just the first
        connection for the provider)."""
        result = await session.execute(
            text(
                """
                INSERT INTO source_events
                    (workspace_id, provider, event_type, source_id,
                     external_event_id, source_connection_id, payload, outcome, sweep_id)
                VALUES
                    (:workspace_id, CAST(:provider AS source_provider), :event_type, :source_id,
                     :external_event_id, :source_connection_id, CAST(:payload AS jsonb), 'queued',
                     CAST(:sweep_id AS uuid))
                ON CONFLICT ON CONSTRAINT source_events_workspace_provider_event_key
                DO NOTHING
                RETURNING id
                """
            ).bindparams(
                workspace_id=workspace_id,
                provider=event.provider,
                event_type=event.event_type,
                source_id=event.source_id,
                external_event_id=event.external_event_id,
                source_connection_id=source_connection_id,
                payload=json.dumps(event.raw),
                sweep_id=sweep_id,
            )
        )
        row = result.first()
        return str(row.id) if row else None

    async def advance_sync(
        self,
        session: AsyncSession,
        source_id: str,
        synced_at: datetime | None,
        sync_cursor: str | None = None,
        mark_backfilled: bool = True,
    ) -> None:
        # ``sync_cursor`` is the opaque per-connection cursor for token-cursor
        # providers (Drive pageToken / Gmail historyId); NULL leaves it untouched so
        # timestamp providers (Notion/GitHub) keep relying on last_synced_at.
        #
        # ``backfilled_at`` records that this connection's *history* has been imported
        # (migration 0022) — the predicate behind ``needs_backfill`` on GET /sources.
        # It is stamped on the first run that finishes without a continuation cursor,
        # so Drive/Gmail's chained backfill only counts once the chain is exhausted
        # (``mark_backfilled=False`` while more chunks are pending). The IS NULL guard
        # makes it idempotent: later incremental syncs never move the timestamp, so it
        # stays the moment history landed rather than becoming a second last_synced_at.
        await session.execute(
            text(
                """
                UPDATE source_connections
                   SET last_synced_at = COALESCE(:synced_at, last_synced_at),
                       sync_cursor = COALESCE(:sync_cursor, sync_cursor),
                       sync_status = 'healthy',
                       status = 'connected',
                       backfilled_at = CASE
                           WHEN :mark_backfilled AND backfilled_at IS NULL THEN now()
                           ELSE backfilled_at
                       END,
                       updated_at = now()
                 WHERE id = :id
                """
            ).bindparams(
                id=source_id,
                synced_at=synced_at,
                sync_cursor=sync_cursor,
                mark_backfilled=mark_backfilled,
            )
        )

    async def mark_syncing(self, session: AsyncSession, source_ids: list[str]) -> None:
        """Flag connections as actively importing (the sweep is queued for them).

        ``'syncing'`` is a long-declared ``sync_status`` value that nothing wrote until
        now. It self-clears: ``advance_sync`` flips it to ``'healthy'`` and ``mark_error``
        to ``'error'``. ``needs_backfill`` treats it as in-progress for one hour only, so
        a dropped enqueue (``jobs/queue.py`` swallows Redis outages) can't hide the
        connection's import button forever.
        """
        if not source_ids:
            return
        await session.execute(
            text(
                """
                UPDATE source_connections
                   SET sync_status = 'syncing', updated_at = now()
                 WHERE id = ANY(:ids)
                """
            ).bindparams(ids=source_ids)
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

    async def resolve_all_by_account(
        self, session: AsyncSession, provider: str, external_account_id: str
    ) -> list[tuple[str, str]]:
        """Every ``(source_id, workspace_id)`` for a provider account (webhook routing).

        The same provider account (Slack team, Gmail mailbox, GitHub installation) can be
        connected in more than one workspace. A single webhook delivery must fan out to
        *all* of them — matching only the first would leave the others permanently stale
        (push providers are never polled). Runs on a service-role session (no tenant
        context yet), so it deliberately reads across workspaces.
        """
        rows = (
            await session.execute(
                text(
                    """
                    SELECT id, workspace_id FROM source_connections
                     WHERE provider = CAST(:provider AS source_provider)
                       AND external_account_id = :account
                    """
                ).bindparams(provider=provider, account=external_account_id)
            )
        ).all()
        return [(r.id, r.workspace_id) for r in rows]

    async def list_pollable_connections(
        self, session: AsyncSession, providers: list[str]
    ) -> list[tuple[str, str]]:
        """``(source_id, workspace_id)`` for every connected connection of ``providers``.

        Cross-tenant by design (like ``list_expiring_subscriptions``): the polling
        cron fans out one tenant-scoped ``source_sync`` job per row.
        """
        rows = (
            await session.execute(
                text(
                    """
                    SELECT id, workspace_id FROM source_connections
                     WHERE status = 'connected'
                       AND provider::text = ANY(:providers)
                    """
                ).bindparams(providers=providers)
            )
        ).all()
        return [(r.id, r.workspace_id) for r in rows]

    async def list_stale_queued_events(
        self, session: AsyncSession, *, older_than: str, limit: int
    ) -> list[tuple[str, str, str | None]]:
        """``(event_id, workspace_id, sweep_id)`` for events stranded at
        ``outcome='queued'`` past a grace window (cross-tenant, service role).

        Backstop for the enqueue-after-commit gap: an event row is committed
        ``queued`` and then its follow-up ``extract_event`` may never fire (Redis
        outage, chained backfill, sweep_extract timeout). The reconciliation cron
        re-enqueues these; the pipeline's own ``processed`` guard makes a re-run of
        an event that did extract a no-op, so this is safe to run repeatedly.
        """
        rows = (
            await session.execute(
                text(
                    """
                    SELECT id, workspace_id, sweep_id FROM source_events
                     WHERE processed = FALSE
                       AND outcome = 'queued'
                       AND created_at < now() - CAST(:older_than AS interval)
                     ORDER BY created_at
                     LIMIT :limit
                    """
                ).bindparams(older_than=older_than, limit=limit)
            )
        ).all()
        return [
            (str(r.id), r.workspace_id, str(r.sweep_id) if r.sweep_id else None)
            for r in rows
        ]

    # ── onboarding sweeps ──────────────────────────────────────────────────────────
    async def list_connected_sources(self, session: AsyncSession) -> list[dict]:
        """All connected connections in the tenant, oldest first (stable sweep order)."""
        rows = (
            await session.execute(
                text(
                    """
                    SELECT id, provider FROM source_connections
                     WHERE status = 'connected'
                     ORDER BY created_at
                    """
                )
            )
        ).mappings().all()
        return [dict(r) for r in rows]

    async def selected_channel_ids(self, session: AsyncSession, source_id: str) -> list[str]:
        """External ids of the channels selected for ingestion (empty = no selection)."""
        rows = (
            await session.execute(
                text(
                    """
                    SELECT external_id FROM source_channels
                     WHERE source_id = :source_id
                       AND selected
                       AND external_id IS NOT NULL
                    """
                ).bindparams(source_id=source_id)
            )
        ).scalars().all()
        return list(rows)

    async def get_sweep(self, session: AsyncSession, sweep_id: str) -> dict | None:
        row = (
            await session.execute(
                text(
                    """
                    SELECT id, status, config, progress, skills_created, skills_queued,
                           started_at, completed_at
                      FROM sweeps
                     WHERE id = CAST(:id AS uuid)
                    """
                ).bindparams(id=sweep_id)
            )
        ).mappings().first()
        return dict(row) if row else None

    async def set_sweep_status(
        self, session: AsyncSession, sweep_id: str, status: str, *, completed: bool = False
    ) -> None:
        await session.execute(
            text(
                """
                UPDATE sweeps
                   SET status = :status,
                       completed_at = CASE WHEN :completed THEN now() ELSE completed_at END
                 WHERE id = CAST(:id AS uuid)
                """
            ).bindparams(id=sweep_id, status=status, completed=completed)
        )

    async def update_sweep_source_progress(
        self, session: AsyncSession, sweep_id: str, provider: str, progress: dict
    ) -> None:
        """Write one provider's progress into ``sweeps.progress`` (keyed by provider)."""
        await session.execute(
            text(
                """
                UPDATE sweeps
                   SET progress = jsonb_set(
                           COALESCE(progress, '{}'::jsonb),
                           ARRAY[:provider],
                           CAST(:progress AS jsonb)
                       )
                 WHERE id = CAST(:id AS uuid)
                """
            ).bindparams(id=sweep_id, provider=provider, progress=json.dumps(progress))
        )
