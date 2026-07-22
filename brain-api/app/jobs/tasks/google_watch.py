"""Google push-channel lifecycle jobs (KAN-2).

Google watch channels (Drive ``changes.watch``, Gmail ``users.watch``) expire in
<= 7 days and must be re-created to keep receiving push notifications. These jobs
open a channel after a connection is made (``watch_register``) and re-open channels
nearing expiry on a daily cron (``watch_renew``).

Notifications themselves are content-free; the receiver maps a delivery to its
connection and enqueues ``source_sync`` (see ``app/modules/webhooks/service.py``).
Renewal just opens a fresh channel — the superseded one expires on its own (any
duplicate deliveries are absorbed by the idempotent event insert).
"""
from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime, timedelta

from app.config.database import get_session, get_tenant_session
from app.config.settings import settings
from app.integrations import get_integration
from app.integrations.google_common import GOOGLE_PUSH_PROVIDERS
from app.jobs.repository import JobsRepository
from app.jobs.tasks.source_sync import _resolve_token
from app.shared.helpers.crypto import encrypt
from app.shared.helpers.ids import generate_id
from app.shared.middleware.with_tenant import run_in_tenant

log = logging.getLogger(__name__)

_repo = JobsRepository()
_RENEW_WINDOW = timedelta(hours=24)  # renew channels expiring within a day


def _callback_url(provider: str) -> str:
    return f"{settings.oauth_redirect_base_url}/api/v1/webhooks/{provider}"


async def _open_channel(session, workspace_id: str, source_id: str) -> tuple[str, bytes | None, datetime | None]:
    """Resolve the connection token and open a watch channel.

    Returns ``(source_ref_id, secret_enc, expires_at)``. ``secret_enc`` is the
    per-channel token (Drive) or None (Gmail, verified by the Pub/Sub URL token).
    """
    state = await _repo.get_sync_state(session, source_id)
    if state is None or state.access_token_enc is None:
        raise RuntimeError(f"watch: connection {source_id} missing or tokenless")
    integration = get_integration(state.provider)
    access_token = await _resolve_token(session, state)

    token = secrets.token_urlsafe(32)
    ref, expires_at = await integration.register_watch(
        access_token, callback_url=_callback_url(state.provider), token=token
    )
    # Drive routes by channel id + verifies the per-channel token; Gmail routes by
    # account email + verifies the global Pub/Sub URL token, so it needs no secret.
    secret_enc = encrypt(token).encode() if state.provider == "google_drive" else None
    return ref, secret_enc, expires_at


async def watch_register(ctx: dict, workspace_id: str, source_id: str) -> dict:
    """Open a push channel for a freshly connected Google source."""
    async with get_tenant_session() as session:
        async with run_in_tenant(session, workspace_id, "system", "admin"):
            ref, secret_enc, expires_at = await _open_channel(session, workspace_id, source_id)
            await _repo.insert_subscription(
                session,
                sub_id=generate_id("webhook"),
                workspace_id=workspace_id,
                provider=(await _repo.get_sync_state(session, source_id)).provider,
                source_ref_id=ref,
                target_id=source_id,
                secret_enc=secret_enc,
                expires_at=expires_at,
            )
            await session.commit()
    log.info("watch_register: opened channel for %s (expires %s)", source_id, expires_at)
    return {"registered": source_id, "expires_at": expires_at.isoformat() if expires_at else None}


async def watch_renew(ctx: dict) -> dict:
    """Daily cron: re-open Google watch channels expiring within 24h."""
    before = datetime.now(UTC) + _RENEW_WINDOW
    async with get_session() as session:
        expiring = await _repo.list_expiring_subscriptions(session, before)

    renewed = 0
    for sub_id, workspace_id, provider, source_id in expiring:
        if provider not in GOOGLE_PUSH_PROVIDERS:
            continue
        try:
            async with get_tenant_session() as session:
                async with run_in_tenant(session, workspace_id, "system", "admin"):
                    ref, secret_enc, expires_at = await _open_channel(
                        session, workspace_id, source_id
                    )
                    await _repo.update_subscription(
                        session,
                        sub_id,
                        source_ref_id=ref,
                        secret_enc=secret_enc,
                        expires_at=expires_at,
                    )
                    await session.commit()
            renewed += 1
        except Exception:  # noqa: BLE001 — one bad channel must not stall the rest
            log.exception("watch_renew: failed to renew %s (%s)", source_id, provider)
    return {"renewed": renewed}
