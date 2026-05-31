from uuid import UUID


async def run_sweep(sweep_id: UUID) -> None:
    """
    Background job: process historical content from all connected sources
    in authority priority order (Notion → GitHub → Jira → Slack → Zendesk).

    Key properties:
      - All sweep extractions go to review_queue regardless of confidence score
        (auto_publish_during_sweep=false per source_authority.yaml)
      - Rate-limited: settings.sweep_rate_per_minute items per source per minute
      - Concurrent LLM calls capped by asyncio.Semaphore(settings.semaphore_limit)
      - Resumable: picks up from last processed item using sweep_id on source_events

    Progress is tracked per source in sweeps.progress JSONB column.
    """
    raise NotImplementedError("Phase 2")
