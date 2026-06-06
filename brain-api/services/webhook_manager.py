from __future__ import annotations

import logging

from connectors.base import SourceConnector
from connectors.github import GitHubConnector
from connectors.jira import JiraConnector
from connectors.notion import NotionConnector
from connectors.slack import SlackConnector
from connectors.zendesk import ZendeskConnector

log = logging.getLogger(__name__)

_CONNECTOR_CLASSES: dict[str, type[SourceConnector]] = {
    "notion": NotionConnector,
    "slack": SlackConnector,
    "github": GitHubConnector,
    "jira": JiraConnector,
    "zendesk": ZendeskConnector,
}


def _get_connector(source: str, access_token: str) -> SourceConnector:
    cls = _CONNECTOR_CLASSES.get(source)
    if cls is None:
        raise ValueError(f"Unknown source: {source}")
    return cls(access_token)


async def subscribe(
    pool,
    source: str,
    target_id: str,
    access_token: str,
    callback_url: str,
) -> dict:
    """
    Register a webhook subscription with the source and record it in the DB.
    Returns the inserted webhook_subscriptions row as a dict.
    """
    connector = _get_connector(source, access_token)
    try:
        source_ref_id = await connector.subscribe_webhook(target_id, callback_url)
    except NotImplementedError:
        source_ref_id = f"stub-{source}-{target_id}"
        log.warning("Connector %s.subscribe_webhook not implemented — using stub id", source)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO webhook_subscriptions (source, source_ref_id, target_id) "
            "VALUES ($1, $2, $3) "
            "RETURNING id, source, source_ref_id, target_id, status, created_at",
            source, source_ref_id, target_id,
        )
    return dict(row)


async def unsubscribe(
    pool,
    source: str,
    target_id: str,
    access_token: str,
) -> None:
    """
    Revoke the active webhook subscription for source+target_id.
    Idempotent — no-ops if no active subscription exists.
    """
    connector = _get_connector(source, access_token)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, source_ref_id FROM webhook_subscriptions "
            "WHERE source=$1 AND target_id=$2 AND status='active'",
            source, target_id,
        )

    if row is None:
        return

    try:
        await connector.unsubscribe_webhook(row["source_ref_id"])
    except NotImplementedError:
        log.warning("Connector %s.unsubscribe_webhook not implemented — marking revoked anyway", source)

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE webhook_subscriptions SET status='revoked' WHERE id=$1",
            row["id"],
        )
