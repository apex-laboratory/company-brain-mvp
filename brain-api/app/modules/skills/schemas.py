"""Skills delivery schemas (Phase 5 — PRD §14 API Surface, Feature 15/15a).

The agent-facing read surface: semantic search, full skill body, version history,
and the interaction-override feedback loop. Responses serialize camelCase via the
shared ``CamelModel``; requests reject unknown keys via ``CamelRequestModel``.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import Field

from app.shared.schemas import CamelModel, CamelRequestModel


class SkillSearchResult(CamelModel):
    """One semantic-search hit. ``similarity`` is cosine similarity (0–1).

    ``status`` is always a published state (``active``/``stable``) — search only
    returns published skills — so the FE can render an honest badge instead of a
    default. ``calls30d``/``callSeries`` mirror the list item so the registry table
    renders the same metrics for search hits and browse rows."""

    id: str
    name: str
    version: str
    status: str | None = None
    base_logic: str
    exceptions_block: list = Field(default_factory=list)
    source_authority: str | None = None
    similarity: float
    calls30d: int = Field(0, alias="calls30d")  # to_camel would mangle to calls30D
    call_series: list[int] = Field(default_factory=list)
    updated_at: datetime | None = None


class SkillListItem(CamelModel):
    """One row of the browse-the-registry list (``GET /skills``).

    Same shape as ``SkillSearchResult`` minus ``similarity`` — the FE shares one
    view model across search and browse. ``callSeries`` is a 7-point daily
    sparkline (oldest first); ``calls30d`` and ``updatedAt`` come from the skill row."""

    id: str
    name: str
    version: str
    status: str
    base_logic: str | None = None
    exceptions_block: list = Field(default_factory=list)
    source_authority: str | None = None
    source_providers: list[str] = Field(default_factory=list)
    description: str | None = None
    calls30d: int = Field(0, alias="calls30d")  # to_camel would mangle to calls30D
    call_series: list[int] = Field(default_factory=list)
    updated_at: datetime | None = None


class SkillStats(CamelModel):
    """Registry summary strip (mirrors ``/reviews/stats``)."""

    total: int
    stable: int
    in_review: int
    draft: int
    calls30d: int = Field(alias="calls30d")  # to_camel would mangle to calls30D


class SkillOut(CamelModel):
    """Full skill body served to agents and the dashboard."""

    id: str
    name: str
    version: str
    status: str
    trigger: str | None = None
    base_logic: str | None = None
    exceptions_block: list = Field(default_factory=list)
    actions: list = Field(default_factory=list)
    source_authority: str | None = None
    confidence: float | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class SkillVersionOut(CamelModel):
    """One historical version from ``skill_versions``."""

    version: str
    base_logic: str | None = None
    exceptions_block: list = Field(default_factory=list)
    confidence: float | None = None
    change_type: str | None = None
    created_at: datetime | None = None


class CreateSkillRequest(CamelRequestModel):
    """Manually author a skill from the dashboard (admin-only).

    The skill lands at status ``draft`` and opens a review so a human still
    confirms it before it becomes agent-queryable — the same gate the extraction
    pipeline goes through."""

    name: Annotated[str, Field(min_length=1, max_length=200)]
    trigger: Annotated[str | None, Field(max_length=2000)] = None
    base_logic: Annotated[str, Field(min_length=1, max_length=20_000)]
    description: Annotated[str | None, Field(max_length=2000)] = None


class SubmitForReviewRequest(CamelRequestModel):
    """Send a draft skill to the human review queue.

    ``note`` is context for the reviewer (why this draft is worth publishing); it
    is stored on the review's payload, not on the skill."""

    note: Annotated[str | None, Field(max_length=2000)] = None


class SubmitForReviewResult(CamelModel):
    """Outcome of ``POST /skills/{id}/submit``.

    ``reviewCreated`` is ``False`` when the draft already had an open review (a
    hand-authored skill opens one at creation) — the existing card is reused, so
    ``reviewId`` still points at the item the reviewer will see."""

    skill_id: str
    status: str
    review_id: str
    review_created: bool


class OverrideRequest(CamelRequestModel):
    """Report that an agent overrode a matched skill (Feature 15a feedback loop)."""

    reason: str | None = None


class OverrideResult(CamelModel):
    interaction_id: str
    skill_id: str | None = None
    new_confidence: float | None = None
    review_created: bool = False
