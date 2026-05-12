"""
Ingestion endpoints.

The Zendesk routes accept records from a post-Airbyte normalization driver
(or a local fixture loader) and write contract-compliant rows into
`raw_content`. PM4Py event-log inspection is exposed under
`/ingest/process-mining/` so downstream phases can preview the log without
running a full mining pass.

`POST /ingest/event` (living-currency webhook) and `POST /ingest/batch`
(full extraction trigger) remain Phase 3/5 work — left as 501.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from database import get_pool
from models.schemas import (
    EventLogResponse,
    IngestResult,
    ZendeskBatchPayload,
    ZendeskComment,
    ZendeskTicket,
    ZendeskTicketEvent,
)
from services import process_miner, zendesk_normalizer


router = APIRouter()


FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "zendesk_sample.json"


@router.post("/event")
async def ingest_event():
    raise HTTPException(status_code=501, detail="Not implemented — Phase 5")


@router.post("/batch")
async def ingest_batch():
    raise HTTPException(status_code=501, detail="Not implemented — Phase 3")


# --- Zendesk -------------------------------------------------------------- #

@router.post("/zendesk/tickets", response_model=IngestResult)
async def ingest_zendesk_tickets(
    tickets: list[ZendeskTicket],
    subdomain: str | None = Query(default=None),
) -> IngestResult:
    pool = await get_pool()
    result = await zendesk_normalizer.ingest_batch(
        pool, tickets=tickets, subdomain=subdomain
    )
    return IngestResult(**result)


@router.post("/zendesk/comments", response_model=IngestResult)
async def ingest_zendesk_comments(
    comments: list[ZendeskComment],
    subdomain: str | None = Query(default=None),
) -> IngestResult:
    pool = await get_pool()
    result = await zendesk_normalizer.ingest_batch(
        pool, comments=comments, subdomain=subdomain
    )
    return IngestResult(**result)


@router.post("/zendesk/events", response_model=IngestResult)
async def ingest_zendesk_events(
    events: list[ZendeskTicketEvent],
    subdomain: str | None = Query(default=None),
) -> IngestResult:
    pool = await get_pool()
    result = await zendesk_normalizer.ingest_batch(
        pool, events=events, subdomain=subdomain
    )
    return IngestResult(**result)


@router.post("/zendesk", response_model=IngestResult)
async def ingest_zendesk_batch(payload: ZendeskBatchPayload) -> IngestResult:
    """Combined endpoint — accepts tickets, comments, and events in one call."""
    pool = await get_pool()
    result = await zendesk_normalizer.ingest_batch(
        pool,
        tickets=payload.tickets,
        comments=payload.comments,
        events=payload.events,
        subdomain=payload.subdomain,
    )
    return IngestResult(**result)


@router.post("/zendesk/sample", response_model=IngestResult)
async def ingest_zendesk_sample() -> IngestResult:
    """Load the bundled Zendesk fixture — convenience for local verification."""
    if not FIXTURE_PATH.exists():
        raise HTTPException(
            status_code=500,
            detail=f"Zendesk fixture not found at {FIXTURE_PATH}",
        )
    payload: dict[str, Any] = json.loads(FIXTURE_PATH.read_text())
    pool = await get_pool()
    result = await zendesk_normalizer.ingest_batch(
        pool,
        tickets=payload.get("tickets", []),
        comments=payload.get("comments", []),
        events=payload.get("events", []),
        subdomain=payload.get("subdomain"),
    )
    return IngestResult(**result)


# --- Process mining preview ---------------------------------------------- #

@router.get("/process-mining/event-log", response_model=EventLogResponse)
async def get_event_log(
    ticket_source_id: list[str] | None = Query(default=None),
) -> EventLogResponse:
    """
    Return the PM4Py-shaped event log built from Zendesk ticket events.
    Filter by passing one or more `ticket_source_id=ticket:<id>` params.
    """
    pool = await get_pool()
    payload = await process_miner.event_log_as_json(pool, ticket_source_id)
    return EventLogResponse(**payload)


@router.get("/process-mining/variants")
async def get_variants(
    ticket_source_id: list[str] | None = Query(default=None),
) -> dict[str, Any]:
    """Return variant counts for the ticket event log (lightweight preview)."""
    pool = await get_pool()
    return await process_miner.mine_zendesk_patterns(pool, ticket_source_id)
