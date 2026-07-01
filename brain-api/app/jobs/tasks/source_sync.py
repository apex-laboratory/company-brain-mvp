"""``source_sync`` job — incremental sweep of one source connection (KAN-2).

Runs in the connection's tenant context (RLS applies to the worker). Decrypts the
stored token, refreshes it if it's near expiry, fetches everything changed since
the connection cursor, normalizes each item to a :class:`RawEvent`, and inserts it
idempotently into ``source_events``. The cursor is connection-level
(``source_connections.last_synced_at``) — exactly Notion's ``last_edited_time``
model. (Per-channel cursors are a later enhancement for channel-scoped providers.)

Errors are classified: a 401 marks the connection ``error`` (re-auth needed) and
does not retry; transient failures re-raise so ARQ retries with backoff.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import httpx

from app.config.database import get_session
from app.integrations import get_integration
from app.integrations.base import ChannelRef, ConnectorAuthError
from app.jobs.repository import JobsRepository, SyncState
from app.shared.helpers.crypto import decrypt
from app.shared.middleware.with_tenant import run_in_tenant

log = logging.getLogger(__name__)

_repo = JobsRepository()
_REFRESH_SKEW = timedelta(minutes=5)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


async def _resolve_token(state: SyncState) -> str:
    """Decrypt the access token, refreshing proactively when near expiry."""
    integration = get_integration(state.provider)
    token = decrypt(state.access_token_enc.decode())  # type: ignore[union-attr]
    expires = state.token_expires_at
    if expires and expires < datetime.now(UTC) + _REFRESH_SKEW and state.refresh_token_enc:
        refreshed = await integration.refresh(decrypt(state.refresh_token_enc.decode()))
        token = refreshed.access_token
        # Persisting rotated tokens lands with refresh-capable providers; Notion
        # tokens don't expire, so this branch is inert for the first connector.
    return token


async def source_sync(ctx: dict, workspace_id: str, source_id: str) -> dict:
    """ARQ entrypoint. ``ctx`` is the ARQ job context (unused)."""
    integration = None
    inserted = 0
    async with get_session() as session:
        async with run_in_tenant(session, workspace_id, "system", "admin"):
            state = await _repo.get_sync_state(session, source_id)
            if state is None or state.access_token_enc is None:
                log.warning("source_sync: connection %s missing or tokenless", source_id)
                return {"inserted": 0, "skipped": "no_connection"}

            integration = get_integration(state.provider)
            try:
                token = await _resolve_token(state)
                channel = ChannelRef(
                    external_id=state.external_account_id or "workspace", name="workspace"
                )
                cursor = state.last_synced_at.isoformat() if state.last_synced_at else None
                items, next_cursor = await integration.fetch_since(token, channel, cursor)

                for item in items:
                    event = integration.normalize(item)
                    if await _repo.insert_event(session, workspace_id, event):
                        inserted += 1

                await _repo.advance_sync(session, source_id, _parse_iso(next_cursor))
                await session.commit()
            except httpx.HTTPStatusError as exc:
                auth_broken = exc.response.status_code in (401, 403)
                await _repo.mark_error(session, source_id, auth_broken=auth_broken)
                await session.commit()
                if auth_broken:
                    log.warning("source_sync: %s auth broken — re-auth required", source_id)
                    return {"inserted": inserted, "error": "auth_broken"}
                raise  # transient — let ARQ retry
            except ConnectorAuthError:
                # Provider reported auth failure out-of-band (e.g. Slack ok:false
                # invalid_auth), not via a 401 status — same handling as a 401.
                await _repo.mark_error(session, source_id, auth_broken=True)
                await session.commit()
                log.warning("source_sync: %s auth broken — re-auth required", source_id)
                return {"inserted": inserted, "error": "auth_broken"}
            except Exception:
                await _repo.mark_error(session, source_id, auth_broken=False)
                await session.commit()
                raise

    log.info("source_sync: %s inserted %d new events", source_id, inserted)
    return {"inserted": inserted}
