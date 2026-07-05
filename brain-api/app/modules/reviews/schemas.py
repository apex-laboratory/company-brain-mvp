"""Reviews request/response schemas (BACKEND_BEST_PRACTICES.md §3, §5).

Responses serialize to camelCase. The review queue drives human approval of
pipeline-extracted skills; the UI (Phase 4) renders these cards, but the approve/
reject decision itself is API-only in Phase 3.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


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


class ReviewStats(_Camel):
    pending: int
    approved: int
    rejected: int
    rejection_rate: float  # rejected / (approved + rejected), 0.0 when none resolved
