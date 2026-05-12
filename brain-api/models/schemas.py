from __future__ import annotations
from typing import Any, Literal
from uuid import UUID
from datetime import datetime
from pydantic import BaseModel, Field


class SkillCreate(BaseModel):
    name: str
    description: str | None = None
    decision_logic: str | None = None
    tool_schemas: list[dict[str, Any]] | None = None
    confidence: float | None = None


class SkillRead(BaseModel):
    id: UUID
    name: str
    version: int
    confidence: float | None
    description: str | None
    decision_logic: str | None
    tool_schemas: Any | None
    source_ids: Any
    conflict_flags: Any
    graph_node_ids: Any
    status: str
    created_at: datetime
    updated_at: datetime


class SkillUpdate(BaseModel):
    description: str | None = None
    decision_logic: str | None = None
    tool_schemas: list[dict[str, Any]] | None = None
    confidence: float | None = None
    status: str | None = None


class IngestEvent(BaseModel):
    source: str
    source_id: str
    content: str
    metadata: dict[str, Any] | None = None


class ReviewAction(BaseModel):
    reviewer_id: str | None = None
    reason: str | None = None


# --- Zendesk payload models ---
#
# Mirrors the shapes returned by the Zendesk REST API and the Airbyte
# `tickets` / `ticket_comments` / `ticket_events` streams. We keep them
# permissive (`extra="allow"`) so unexpected fields land in metadata
# without breaking normalization.


class ZendeskUser(BaseModel):
    model_config = {"extra": "allow"}

    id: int | str | None = None
    name: str | None = None
    email: str | None = None
    role: str | None = None


class ZendeskTicket(BaseModel):
    """A Zendesk ticket as returned by /api/v2/tickets.json or the Airbyte stream."""

    model_config = {"extra": "allow"}

    id: int | str
    subject: str | None = None
    description: str | None = None
    status: str | None = None
    priority: str | None = None
    type: str | None = None
    tags: list[str] = Field(default_factory=list)
    assignee_id: int | str | None = None
    requester_id: int | str | None = None
    submitter_id: int | str | None = None
    ticket_form_id: int | str | None = None
    organization_id: int | str | None = None
    group_id: int | str | None = None
    via: dict[str, Any] | None = None
    url: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    subdomain: str | None = None


class ZendeskComment(BaseModel):
    """A Zendesk ticket comment."""

    model_config = {"extra": "allow"}

    id: int | str
    ticket_id: int | str
    body: str | None = None
    plain_body: str | None = None
    html_body: str | None = None
    public: bool | None = None
    author_id: int | str | None = None
    via: dict[str, Any] | None = None
    created_at: datetime | None = None
    audit_id: int | str | None = None
    author_role: str | None = None
    subdomain: str | None = None


class ZendeskTicketEvent(BaseModel):
    """
    A single field-level change on a ticket. Sourced from Zendesk's
    `ticket_events` / `incremental/ticket_events` streams or unrolled
    from a ticket audit's `child_events`.

    Each instance represents ONE field transition so PM4Py can treat
    it as a discrete activity in the event log.
    """

    model_config = {"extra": "allow"}

    id: int | str = Field(description="Stable child-event identifier")
    ticket_id: int | str
    field_name: str = Field(description="e.g. status, priority, assignee_id, group_id")
    previous_value: Any | None = None
    new_value: Any | None = None
    event_type: str | None = Field(
        default=None,
        description="Zendesk event_type if present (Change, Create, Comment, ...)",
    )
    author_id: int | str | None = None
    audit_id: int | str | None = None
    via: dict[str, Any] | None = None
    created_at: datetime | None = Field(
        default=None,
        description="Wall-clock time the change happened — used as PM4Py timestamp",
    )
    subdomain: str | None = None


class ZendeskBatchPayload(BaseModel):
    """Bulk payload variant accepted by the ingest endpoints."""

    subdomain: str | None = None
    tickets: list[ZendeskTicket] = Field(default_factory=list)
    comments: list[ZendeskComment] = Field(default_factory=list)
    events: list[ZendeskTicketEvent] = Field(default_factory=list)


class IngestResult(BaseModel):
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    by_entity: dict[str, int] = Field(default_factory=dict)


class EventLogRow(BaseModel):
    case_id: str
    activity: str
    timestamp: datetime
    field_name: str | None = None
    previous_value: str | None = None
    new_value: str | None = None
    author_id: str | None = None
    source_id: str


class EventLogResponse(BaseModel):
    rows: list[EventLogRow]
    cases: int
    activities: list[str]
    format: Literal["pm4py-dataframe"] = "pm4py-dataframe"
