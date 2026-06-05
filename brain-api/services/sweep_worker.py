from __future__ import annotations

import asyncio
import json
import logging
from uuid import UUID

from connectors.base import SourceConnector
from connectors.github import GitHubConnector
from connectors.jira import JiraConnector
from connectors.notion import NotionConnector
from connectors.slack import SlackConnector
from connectors.zendesk import ZendeskConnector
from database import get_pool
from models.schemas import SweepSourceProgress
from services.authority_annotator import sweep_processing_order, sweep_rate_per_minute, sweep_semaphore_limit
from services.pipeline import PipelineResult, SourceEvent, run_pipeline

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


async def _fetch_and_store(conn, sweep_id: UUID, source: str, connector: SourceConnector,
                           target_id: str, lookback_days: int) -> int:
    """Fetch historical items for one target_id and insert into source_events. Returns insert count."""
    existing = await conn.fetchval(
        "SELECT COUNT(*) FROM source_events WHERE sweep_id=$1 AND source=$2 AND payload->>'_target_id'=$3",
        sweep_id, source, target_id,
    )
    if existing:
        return 0

    try:
        items = await connector.fetch_historical(target_id, lookback_days)
    except NotImplementedError:
        log.warning("Connector %s.fetch_historical not implemented — skipping target %s", source, target_id)
        return 0

    for item in items:
        payload = {**item, "_target_id": target_id}
        await conn.execute(
            "INSERT INTO source_events (source, event_type, source_id, payload, sweep_id) "
            "VALUES ($1, 'sweep_historical', $2, $3::jsonb, $4)",
            source, str(item.get("id", "")), json.dumps(payload), sweep_id,
        )

    return len(items)


async def _process_items(pool, sweep_id: UUID, source: str,
                         semaphore: asyncio.Semaphore, rate_interval: float) -> SweepSourceProgress:
    """Process all unprocessed source_events for sweep+source. Returns final progress."""
    progress = SweepSourceProgress()

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, event_type, source_id, payload FROM source_events "
            "WHERE sweep_id=$1 AND source=$2 AND processed=FALSE",
            sweep_id, source,
        )

    for row in rows:
        event = SourceEvent(
            source=source,
            event_type=row["event_type"],
            source_id=row["source_id"],
            payload=dict(row["payload"]),
            db_event_id=row["id"],
            sweep_id=sweep_id,
        )

        try:
            async with semaphore:
                result = await run_pipeline(event)
        except NotImplementedError:
            result = PipelineResult(outcome="discarded")
        except Exception:
            log.exception("Pipeline error for %s event %s", source, row["id"])
            result = PipelineResult(outcome="discarded")

        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE source_events SET processed=TRUE, outcome=$1, skill_id=$2 WHERE id=$3",
                result.outcome, result.skill_id, row["id"],
            )

        progress.processed += 1
        if result.outcome == "published":
            progress.published += 1
        elif result.outcome in ("queued", "pending_review"):
            progress.queued += 1
        else:
            progress.discarded += 1

        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE sweeps SET progress = jsonb_set(COALESCE(progress, '{}'), $1::text[], $2::jsonb) "
                "WHERE id=$3",
                [source], json.dumps(progress.model_dump()), sweep_id,
            )

        await asyncio.sleep(rate_interval)

    return progress


async def run_sweep(sweep_id: UUID) -> None:
    """
    Background job: process historical content from all connected sources
    in authority priority order (Notion → GitHub → Jira → Slack → Zendesk).

    Key properties:
      - All sweep extractions go to review_queue regardless of confidence score
        (auto_publish_during_sweep=false per source_authority.yaml)
      - Rate-limited: sweep_rate_per_minute() items per source per minute
      - Concurrent LLM calls capped by asyncio.Semaphore(sweep_semaphore_limit())
      - Resumable: picks up from last processed item using sweep_id on source_events

    Progress is tracked per source in sweeps.progress JSONB column.
    """
    pool = await get_pool()
    semaphore = asyncio.Semaphore(sweep_semaphore_limit())
    rate_interval = 60.0 / sweep_rate_per_minute()
    total_published = 0
    total_queued = 0

    try:
        async with pool.acquire() as conn:
            await conn.execute("UPDATE sweeps SET status='running' WHERE id=$1", sweep_id)
            rows = await conn.fetch(
                "SELECT source, access_token, monitored_ids, lookback_days "
                "FROM source_connections WHERE status='connected'",
            )

        conn_map = {r["source"]: r for r in rows}

        for source in sweep_processing_order():
            if source not in conn_map:
                continue

            conn_row = conn_map[source]
            connector = _get_connector(source, conn_row["access_token"])
            monitored_ids: list[str] = conn_row["monitored_ids"] or []
            lookback_days: int = conn_row["lookback_days"]

            fetch_total = 0
            async with pool.acquire() as conn:
                for target_id in monitored_ids:
                    fetch_total += await _fetch_and_store(
                        conn, sweep_id, source, connector, target_id, lookback_days,
                    )

            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE sweeps SET progress = jsonb_set(COALESCE(progress, '{}'), $1::text[], $2::jsonb) "
                    "WHERE id=$3",
                    [source], json.dumps({"total": fetch_total, "processed": 0,
                                          "published": 0, "queued": 0, "discarded": 0}), sweep_id,
                )

            progress = await _process_items(pool, sweep_id, source, semaphore, rate_interval)
            total_published += progress.published
            total_queued += progress.queued

        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE sweeps SET status='completed', completed_at=NOW(), "
                "skills_created=$1, skills_queued=$2 WHERE id=$3",
                total_published, total_queued, sweep_id,
            )

    except Exception:
        log.exception("Sweep %s failed", sweep_id)
        try:
            async with pool.acquire() as conn:
                await conn.execute("UPDATE sweeps SET status='failed' WHERE id=$1", sweep_id)
        except Exception:
            log.exception("Could not mark sweep %s as failed", sweep_id)
        raise
