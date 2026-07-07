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
from app.integrations.base import BACKFILL_CURSOR_PREFIX, ChannelRef, ConnectorAuthError
from app.jobs.queue import enqueue
from app.jobs.repository import JobsRepository
from app.jobs.token_helper import resolve_token
from app.shared.middleware.with_tenant import run_in_tenant

log = logging.getLogger(__name__)

_repo = JobsRepository()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _is_rate_limited(response: httpx.Response) -> bool:
    """True when an HTTP error is a rate limit (transient), not an auth failure.

    Providers signal rate limiting with a 429, an exhausted ``x-ratelimit-remaining``,
    or a ``retry-after`` header — GitHub in particular overloads 403 for both rate
    limiting and permission errors. httpx header keys are case-insensitive.
    """
    return (
        response.status_code == 429
        or response.headers.get("x-ratelimit-remaining") == "0"
        or "retry-after" in response.headers
    )


async def source_sync(
    ctx: dict, workspace_id: str, source_id: str, sweep_id: str | None = None
) -> dict:
    """ARQ entrypoint. ``ctx`` is the ARQ job context (unused).

    ``sweep_id`` is set when invoked from ``onboarding_sweep``: events are
    stamped with it and extraction is deferred to the batched ``sweep_extract``
    job (M3). Without it (webhook/poll paths) each inserted event enqueues its
    own ``extract_event``.
    """
    integration = None
    inserted = 0
    inserted_ids: list[str] = []
    chain_backfill = False  # opaque-cursor backfill still has more chunks to fetch
    async with get_session() as session:
        async with run_in_tenant(session, workspace_id, "system", "admin"):
            state = await _repo.get_sync_state(session, source_id)
            if state is None or state.access_token_enc is None:
                log.warning("source_sync: connection %s missing or tokenless", source_id)
                return {"inserted": 0, "skipped": "no_connection"}

            integration = get_integration(state.provider)
            # Token-cursor providers (Google: Drive pageToken / Gmail historyId) carry
            # an opaque cursor in sync_cursor; timestamp providers (Notion/GitHub) keep
            # deriving the cursor from last_synced_at.
            opaque_cursor = getattr(integration, "opaque_cursor", False)
            try:
                token = await resolve_token(session, state)
                channel = ChannelRef(
                    external_id=state.external_account_id or "workspace", name="workspace"
                )
                if opaque_cursor:
                    cursor = state.sync_cursor
                else:
                    cursor = state.last_synced_at.isoformat() if state.last_synced_at else None
                    if cursor is None and state.lookback_days:
                        # First sync: seed the cursor from the connection's lookback
                        # window (onboarding's "how far back?"). Opaque-cursor
                        # providers bound their own bootstrap internally.
                        cursor = (
                            datetime.now(UTC) - timedelta(days=state.lookback_days)
                        ).isoformat()

                kwargs: dict = {}
                if getattr(integration, "supports_channel_filter", False):
                    # Channel-scoped providers (Slack) honor the onboarding picker:
                    # sync only selected channels; no selection = all channels.
                    selected = await _repo.selected_channel_ids(session, source_id)
                    if selected:
                        kwargs["allowed_channels"] = set(selected)
                if opaque_cursor and cursor is None and state.lookback_days:
                    # Opaque-cursor providers (Drive/Gmail) bound their bootstrap
                    # backfill themselves — hand them the connection's window.
                    kwargs["lookback_days"] = state.lookback_days
                items, next_cursor = await integration.fetch_since(token, channel, cursor, **kwargs)

                for item in items:
                    event = integration.normalize(item)
                    event_id = await _repo.insert_event(
                        session, workspace_id, event, sweep_id=sweep_id
                    )
                    if event_id:
                        inserted += 1
                        inserted_ids.append(event_id)

                if opaque_cursor:
                    await _repo.advance_sync(
                        session, source_id, datetime.now(UTC), sync_cursor=next_cursor
                    )
                    chain_backfill = bool(
                        next_cursor and next_cursor.startswith(BACKFILL_CURSOR_PREFIX)
                    )
                else:
                    await _repo.advance_sync(session, source_id, _parse_iso(next_cursor))
                await session.commit()
            except httpx.HTTPStatusError as exc:
                # A 401 is always an auth failure. A 403 is ambiguous — GitHub (and
                # others) overload it for rate limiting, which is *transient*. Only a
                # non-rate-limited 403 counts as broken auth; a rate-limited one must
                # retry, or a momentary quota exhaustion would permanently brick the
                # connection (it would flip to status='error' and never be swept again).
                status = exc.response.status_code
                auth_broken = status == 401 or (
                    status == 403 and not _is_rate_limited(exc.response)
                )
                await _repo.mark_error(session, source_id, auth_broken=auth_broken)
                await session.commit()
                if auth_broken:
                    log.warning("source_sync: %s auth broken — re-auth required", source_id)
                    return {"inserted": inserted, "error": "auth_broken"}
                raise  # transient (incl. rate-limited 403) — let ARQ retry
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

    # Non-sweep syncs (webhook backstop / 15-min poll) extract immediately;
    # sweep events wait for the batched, rate-limited sweep_extract pass.
    if sweep_id is None:
        for event_id in inserted_ids:
            await enqueue("extract_event", workspace_id, event_id)

    if chain_backfill:
        # The backfill is bounded per run and stored a continuation cursor; chain the
        # next chunk so onboarding completes without waiting for the next push/poll. Each
        # run makes forward progress, so this terminates when the window is exhausted.
        # A stable job id keeps a duplicate chunk from stacking within the same bucket.
        # ``sweep_id`` is threaded through so chained sweep chunks keep stamping events
        # for the batched sweep_extract pass instead of leaking per-event extractions.
        await enqueue(
            "source_sync", workspace_id, source_id, sweep_id,
            _job_id=f"backfill-chain:{source_id}",
        )
        log.info("source_sync: %s inserted %d events (backfill continues)", source_id, inserted)
        return {"inserted": inserted, "backfill": "continues"}

    log.info("source_sync: %s inserted %d new events", source_id, inserted)
    return {"inserted": inserted}
