"""Reviews request/response schemas (BACKEND_BEST_PRACTICES.md §3, §5).

Responses serialize to camelCase. The review queue drives human approval of
pipeline-extracted skills; the UI (Phase 4) renders these cards, but the approve/
reject decision itself is API-only in Phase 3.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.shared.schemas import CamelRequestModel


class _Camel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ReviewOut(_Camel):
    id: str
    title: str
    kind: str  # policy_change | new_decision | contradiction | exception
    status: str  # pending | approved | rejected
    verdict: str | None = None
    source_provider: str | None = None
    source_location: str | None = None
    before_text: str | None = None
    after_text: str | None = None
    evidence_quote: str | None = None
    evidence_author: str | None = None
    confidence: int | None = None
    payload: dict | None = None
    skill_id: str | None = None
    comment: str | None = None
    resolved_by: str | None = None
    created_at: datetime
    resolved_at: datetime | None = None


class ResolveRequest(_Camel):
    """Optional reviewer note attached to an approve/reject."""

    comment: str | None = None


class ResolveResult(_Camel):
    id: str
    status: Literal["approved", "rejected"]
    verdict: Literal["approve", "reject"]
    skill_id: str | None = None


class WriteRequest(CamelRequestModel):
    """A reviewer-authored correction: the human writes the skill's logic directly.

    Publishes at confidence 1.0 (human-confirmed) regardless of the pipeline's
    original routing — PRD Feature 21 / Process 6 "Write correction"."""

    base_logic: Annotated[str, Field(min_length=1, max_length=20_000)]
    exceptions: list[dict] | None = None  # replaces the block when provided
    comment: str | None = None


class ContradictionResolveRequest(CamelRequestModel):
    """Resolve a contradiction card: pick a source as authoritative, or write one.

    ``source_a`` / ``source_b`` adopt that side's proposed text (from the review's
    ``payload``) as the new base logic; ``write`` requires ``correction``."""

    choice: Literal["source_a", "source_b", "write"]
    correction: WriteRequest | None = None
    comment: str | None = None


class BulkApproveRequest(CamelRequestModel):
    """Approve many sweep-sourced reviews at once (PRD Feature 23)."""

    ids: Annotated[list[str], Field(min_length=1, max_length=200)]
    comment: str | None = None


class BulkApproveItem(_Camel):
    id: str
    status: Literal["approved", "skipped", "error"]
    detail: str | None = None  # reason for skipped/error


class BulkApproveResult(_Camel):
    results: list[BulkApproveItem]
    approved: int
    skipped: int


class ReviewStats(_Camel):
    pending: int
    approved: int
    rejected: int
    rejection_rate: float  # rejected / (approved + rejected), 0.0 when none resolved
    oldest_pending_at: datetime | None = None  # None when nothing is pending
