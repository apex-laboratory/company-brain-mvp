"""``webhook_ingest`` job — normalize + persist one verified webhook event (KAN-2).

The receiver already verified the signature and returned 200; this job does the
durable work off the request path. It resolves the owning workspace from the
provider account in the payload, normalizes to a :class:`RawEvent`, and inserts
idempotently (the unique constraint makes a replayed delivery a no-op).

Mapping a raw payload to ``(external_account_id, RawItem)`` is provider-specific
and lands with each webhook-capable connector. Notion has no webhooks, so this
job has no live producer yet; the routing + idempotency plumbing is in place for
GitHub/Slack/Jira/Zendesk.
"""
from __future__ import annotations

import logging

from app.config.database import get_session
from app.integrations import get_integration
from app.integrations.base import RawItem
from app.jobs.repository import JobsRepository
from app.shared.middleware.with_tenant import run_in_tenant

log = logging.getLogger(__name__)

_repo = JobsRepository()


async def webhook_ingest(ctx: dict, provider: str, payload: dict) -> dict:
    """ARQ entrypoint. ``ctx`` is the ARQ job context (unused)."""
    integration = get_integration(provider)

    account_id = _account_of(provider, payload)
    if account_id is None:
        log.warning("webhook_ingest: %s payload missing account id", provider)
        return {"inserted": 0, "skipped": "no_account"}

    # Resolve owning workspace on a service-role session (no tenant context yet).
    async with get_session() as session:
        resolved = await _repo.resolve_by_account(session, provider, account_id)
    if resolved is None:
        log.warning("webhook_ingest: no connection for %s/%s", provider, account_id)
        return {"inserted": 0, "skipped": "no_connection"}
    _source_id, workspace_id = resolved

    item = RawItem(external_id=str(payload.get("id", "")), payload=payload)
    event = integration.normalize(item)

    # Re-open under tenant context to satisfy RLS on the insert.
    async with get_session() as session:
        async with run_in_tenant(session, workspace_id, "system", "admin"):
            inserted = await _repo.insert_event(session, workspace_id, event)
            await session.commit()

    return {"inserted": int(inserted)}


def _account_of(provider: str, payload: dict) -> str | None:
    """Extract the provider account id used to route the event to a connection."""
    # Per-provider extraction lands with each connector (e.g. Slack team_id,
    # GitHub installation id). Default to a top-level field if present.
    return payload.get("team_id") or payload.get("account_id")
