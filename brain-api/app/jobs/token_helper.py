"""Shared access-token resolution for workers (source_sync + the pipeline expanders).

Decrypts a connection's stored access token and refreshes it proactively when it
is near expiry, persisting the refreshed token (and any rotated refresh token) so
short-lived tokens (Google ~1h) aren't re-minted every call. Extracted from
``source_sync`` so the extraction pipeline's context expanders reuse the exact
same refresh semantics.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_tenant_session
from app.integrations import get_integration
from app.jobs.repository import JobsRepository, SyncState
from app.shared.helpers.crypto import decrypt, encrypt
from app.shared.middleware.with_tenant import run_in_tenant

_repo = JobsRepository()
_REFRESH_SKEW = timedelta(minutes=5)


async def resolve_token(session: AsyncSession, state: SyncState) -> str:
    """Decrypt the access token, refreshing proactively when near expiry.

    A refreshed token is persisted (``update_tokens``) so providers with short-lived
    access tokens (Google: ~1h) don't re-refresh every call, and a rotated refresh
    token is never lost.
    """
    integration = get_integration(state.provider)
    token = decrypt(state.access_token_enc.decode())  # type: ignore[union-attr]
    expires = state.token_expires_at
    if expires and expires < datetime.now(UTC) + _REFRESH_SKEW and state.refresh_token_enc:
        refreshed = await integration.refresh(decrypt(state.refresh_token_enc.decode()))
        token = refreshed.access_token
        await _repo.update_tokens(
            session,
            state.id,
            access_token_enc=encrypt(refreshed.access_token).encode(),
            token_expires_at=refreshed.expires_at,
            refresh_token_enc=(
                encrypt(refreshed.refresh_token).encode() if refreshed.refresh_token else None
            ),
        )
    return token


async def connection_state_by_provider(
    session: AsyncSession, workspace_id: str, provider: str
) -> SyncState | None:
    """Load the connected connection for ``(workspace_id, provider)`` (first if
    several). Used by the expanders, which key off an event's provider, not a
    connection id."""
    row = (
        await session.execute(
            text(
                """
                SELECT id, provider, access_token_enc, refresh_token_enc,
                       token_expires_at, external_account_id, last_synced_at,
                       sync_cursor, lookback_days
                  FROM source_connections
                 WHERE workspace_id = :ws
                   AND provider = CAST(:provider AS source_provider)
                   AND status = 'connected'
                 ORDER BY created_at
                 LIMIT 1
                """
            ).bindparams(ws=workspace_id, provider=provider)
        )
    ).mappings().first()
    return SyncState(**row) if row else None


async def token_for_provider(
    workspace_id: str, provider: str
) -> tuple[str, str | None] | None:
    """Return ``(access_token, external_account_id)`` for a workspace's provider
    connection, refreshing the token if needed. ``None`` if no connection or no
    token is stored (the expander then falls back to raw content)."""
    async with get_tenant_session() as session, run_in_tenant(
        session, workspace_id, "system", "admin"
    ):
        state = await connection_state_by_provider(session, workspace_id, provider)
        if state is None or state.access_token_enc is None:
            return None
        token = await resolve_token(session, state)
        await session.commit()  # persist a refreshed token, if any
        return token, state.external_account_id


async def token_for_connection(
    workspace_id: str, connection_id: str
) -> tuple[str, str | None] | None:
    """Return ``(access_token, external_account_id)`` for the *specific* connection
    that produced an event, refreshing the token if needed. ``None`` if the
    connection is gone or tokenless. Preferred over ``token_for_provider`` so a
    second same-provider connection doesn't expand with the wrong token."""
    async with get_tenant_session() as session, run_in_tenant(
        session, workspace_id, "system", "admin"
    ):
        state = await _repo.get_sync_state(session, connection_id)
        if state is None or state.access_token_enc is None:
            return None
        token = await resolve_token(session, state)
        await session.commit()  # persist a refreshed token, if any
        return token, state.external_account_id
