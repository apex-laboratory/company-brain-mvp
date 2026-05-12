"""
Zendesk → `raw_content` normalization.

Maps Zendesk Support records (tickets, ticket comments, ticket events) into
the canonical `raw_content` row shape defined in `airbyte/raw-content-contract.md`.

This layer sits AFTER Airbyte extraction. The normalizer does not call Zendesk
or Airbyte directly — it consumes already-extracted records (from raw Airbyte
staging tables, HTTP payloads from a scheduled normalization job, or fixtures
for local verification) and writes contract-compliant rows into `raw_content`.

Ticket events preserve transition semantics (`field_name`, `previous_value`,
`new_value`, `source_timestamp`) in metadata so PM4Py can consume them later
without an additional join (see `services/process_miner.py`).
"""

from __future__ import annotations

import html
import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable

from models.schemas import (
    ZendeskComment,
    ZendeskTicket,
    ZendeskTicketEvent,
)


# --- Helpers --------------------------------------------------------------- #

_DEFAULT_AUTHOR: dict[str, Any] = {"id": None, "name": None, "email": None, "handle": None}

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"[ \t]+")


def _iso(ts: datetime | str | None) -> str | None:
    """Coerce a timestamp to ISO 8601 UTC. Returns None if missing."""
    if ts is None:
        return None
    if isinstance(ts, str):
        # Trust ISO strings from Zendesk; they're already RFC3339.
        return ts
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _strip_html(text: str | None) -> str:
    if not text:
        return ""
    text = _HTML_TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = _WHITESPACE_RE.sub(" ", text)
    return text.strip()


def _ticket_url(subdomain: str | None, ticket_id: int | str) -> str:
    if subdomain:
        return f"https://{subdomain}.zendesk.com/agent/tickets/{ticket_id}"
    return f"zendesk://ticket/{ticket_id}"


def _author(actor_id: int | str | None, role: str | None = None) -> dict[str, Any]:
    if actor_id is None and role is None:
        return dict(_DEFAULT_AUTHOR)
    return {
        "id": str(actor_id) if actor_id is not None else None,
        "name": None,
        "email": None,
        "handle": None,
        **({"role": role} if role else {}),
    }


def _stringify_value(value: Any) -> str | None:
    """Render a Zendesk previous/new value as a stable string."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return ",".join(_stringify_value(v) or "" for v in value)
    if isinstance(value, (dict,)):
        return json.dumps(value, sort_keys=True, default=str)
    return str(value)


# --- Normalizers ----------------------------------------------------------- #

def normalize_ticket(ticket: ZendeskTicket | dict[str, Any]) -> dict[str, Any]:
    """Map a Zendesk ticket to a `raw_content` row."""
    if isinstance(ticket, dict):
        ticket = ZendeskTicket.model_validate(ticket)

    subject = (ticket.subject or "").strip()
    body = _strip_html(ticket.description)
    content = f"{subject}\n\n{body}".strip() if subject or body else ""

    metadata: dict[str, Any] = {
        "entity_type": "ticket",
        "record_url": ticket.url or _ticket_url(ticket.subdomain, ticket.id),
        "title": subject or None,
        "author": _author(ticket.requester_id, role="requester"),
        "created_at": _iso(ticket.created_at),
        "updated_at": _iso(ticket.updated_at),
        "tags": list(ticket.tags or []),
        "airbyte_stream": "tickets",
        "status": ticket.status,
        "priority": ticket.priority,
        "assignee": str(ticket.assignee_id) if ticket.assignee_id is not None else None,
        "requester": str(ticket.requester_id) if ticket.requester_id is not None else None,
        "ticket_form_id": ticket.ticket_form_id,
        "organization_id": ticket.organization_id,
        "group_id": ticket.group_id,
        "via": ticket.via,
        "raw_id": str(ticket.id),
    }

    return {
        "source": "zendesk",
        "source_id": f"ticket:{ticket.id}",
        "content": content,
        "metadata": metadata,
    }


def normalize_comment(comment: ZendeskComment | dict[str, Any]) -> dict[str, Any]:
    """Map a Zendesk ticket comment to a `raw_content` row."""
    if isinstance(comment, dict):
        comment = ZendeskComment.model_validate(comment)

    body = (comment.plain_body or comment.body or _strip_html(comment.html_body) or "").strip()

    record_url = (
        f"https://{comment.subdomain}.zendesk.com/agent/tickets/{comment.ticket_id}/comments/{comment.id}"
        if comment.subdomain
        else f"zendesk://ticket/{comment.ticket_id}/comment/{comment.id}"
    )

    metadata: dict[str, Any] = {
        "entity_type": "ticket_comment",
        "record_url": record_url,
        "title": None,
        "author": _author(comment.author_id, role=comment.author_role),
        "created_at": _iso(comment.created_at),
        "updated_at": _iso(comment.created_at),  # comments are immutable in Zendesk
        "tags": [],
        "airbyte_stream": "ticket_comments",
        "parent_source_id": f"ticket:{comment.ticket_id}",
        "is_public": comment.public,
        "via": comment.via,
        "comment_author_role": comment.author_role,
        "audit_id": comment.audit_id,
        "raw_id": str(comment.id),
    }

    return {
        "source": "zendesk",
        "source_id": f"ticket_comment:{comment.ticket_id}:{comment.id}",
        "content": body,
        "metadata": metadata,
    }


def normalize_event(event: ZendeskTicketEvent | dict[str, Any]) -> dict[str, Any]:
    """
    Map a Zendesk ticket event (a field-level change) to a `raw_content` row.

    Each event is one PM4Py activity. The mapping preserves the full transition
    semantics in metadata: `field_name`, `previous_value`, `new_value`, and a
    `source_timestamp` distinct from `created_at` to support process mining.
    """
    if isinstance(event, dict):
        event = ZendeskTicketEvent.model_validate(event)

    previous = _stringify_value(event.previous_value)
    new = _stringify_value(event.new_value)

    if previous is None and new is not None:
        sentence = f"{event.field_name} set to {new}"
    elif previous is not None and new is None:
        sentence = f"{event.field_name} cleared from {previous}"
    elif previous is not None and new is not None:
        sentence = f"{event.field_name} changed from {previous} to {new}"
    else:
        sentence = f"{event.field_name} touched"

    record_url = (
        f"https://{event.subdomain}.zendesk.com/agent/tickets/{event.ticket_id}/audits"
        if event.subdomain
        else f"zendesk://ticket/{event.ticket_id}/event/{event.id}"
    )

    metadata: dict[str, Any] = {
        "entity_type": "ticket_event",
        "record_url": record_url,
        "title": None,
        "author": _author(event.author_id),
        "created_at": _iso(event.created_at),
        "updated_at": _iso(event.created_at),
        "tags": [],
        "airbyte_stream": "ticket_events",
        "parent_source_id": f"ticket:{event.ticket_id}",
        "event_type": event.event_type or "Change",
        "field_name": event.field_name,
        "previous_value": previous,
        "new_value": new,
        "source_timestamp": _iso(event.created_at),
        "audit_id": event.audit_id,
        "via": event.via,
        "raw_id": str(event.id),
    }

    return {
        "source": "zendesk",
        "source_id": f"ticket_event:{event.ticket_id}:{event.id}",
        "content": sentence,
        "metadata": metadata,
    }


# --- Validation ------------------------------------------------------------ #

_REQUIRED_METADATA_KEYS = (
    "entity_type",
    "record_url",
    "author",
    "created_at",
    "updated_at",
    "airbyte_stream",
)


def validate_row(row: dict[str, Any]) -> None:
    """Enforce the contract checklist before write. Raises ValueError on failure."""
    if row.get("source") != "zendesk":
        raise ValueError(f"Expected source=zendesk, got {row.get('source')!r}")
    if not row.get("source_id"):
        raise ValueError("source_id is required")
    if not (row.get("content") or "").strip():
        raise ValueError(f"content is empty for source_id={row['source_id']}")
    metadata = row.get("metadata") or {}
    missing = [k for k in _REQUIRED_METADATA_KEYS if k not in metadata]
    if missing:
        raise ValueError(
            f"metadata missing required keys {missing} for source_id={row['source_id']}"
        )


# --- DB upsert ------------------------------------------------------------- #

_UPSERT_SQL = """
INSERT INTO raw_content (source, source_id, content, metadata, content_type, updated_at)
VALUES ($1, $2, $3, $4::jsonb, NULL, NOW())
ON CONFLICT (source, source_id) DO UPDATE
SET content = EXCLUDED.content,
    metadata = EXCLUDED.metadata,
    updated_at = NOW()
RETURNING (xmax = 0) AS inserted
"""


async def upsert_rows(pool, rows: Iterable[dict[str, Any]]) -> dict[str, int]:
    """
    Upsert canonical rows into `raw_content`. Returns counts of inserted vs. updated.
    Each row must already conform to the canonical contract.
    """
    inserted = 0
    updated = 0
    by_entity: dict[str, int] = {}

    rows = list(rows)
    if not rows:
        return {"inserted": 0, "updated": 0, "by_entity": by_entity}

    async with pool.acquire() as conn:
        async with conn.transaction():
            for row in rows:
                validate_row(row)
                metadata_json = json.dumps(row["metadata"], default=str)
                result = await conn.fetchrow(
                    _UPSERT_SQL,
                    row["source"],
                    row["source_id"],
                    row["content"],
                    metadata_json,
                )
                if result and result["inserted"]:
                    inserted += 1
                else:
                    updated += 1
                entity = (row["metadata"] or {}).get("entity_type", "unknown")
                by_entity[entity] = by_entity.get(entity, 0) + 1

    return {"inserted": inserted, "updated": updated, "by_entity": by_entity}


# --- Bulk entrypoint ------------------------------------------------------- #

async def ingest_batch(
    pool,
    *,
    tickets: list[ZendeskTicket | dict[str, Any]] | None = None,
    comments: list[ZendeskComment | dict[str, Any]] | None = None,
    events: list[ZendeskTicketEvent | dict[str, Any]] | None = None,
    subdomain: str | None = None,
) -> dict[str, int]:
    """Normalize and upsert a heterogeneous batch of Zendesk records."""
    rows: list[dict[str, Any]] = []

    def _with_subdomain(record: Any) -> Any:
        if subdomain and isinstance(record, dict) and "subdomain" not in record:
            record = {**record, "subdomain": subdomain}
        return record

    for t in tickets or []:
        rows.append(normalize_ticket(_with_subdomain(t)))
    for c in comments or []:
        rows.append(normalize_comment(_with_subdomain(c)))
    for e in events or []:
        rows.append(normalize_event(_with_subdomain(e)))

    return await upsert_rows(pool, rows)
