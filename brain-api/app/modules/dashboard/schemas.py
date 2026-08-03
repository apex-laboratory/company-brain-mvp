"""Dashboard request/response schemas (BACKEND_BEST_PRACTICES.md §3, §5, §14).

Responses serialize to camelCase per the API contract (API_DOCUMENTATION.md
§Dashboard API). The query-param model rejects unknown keys and caps ``limit``
at 100 (default 20) per the contract.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.shared.schemas import CamelModel as _CamelModel


# ── query params ────────────────────────────────────────────────────────────
class ActivityQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: Annotated[int, Field(ge=1, le=100)] = 20
    cursor: str | None = None


# ── overview building blocks ────────────────────────────────────────────────
class WorkspaceSummary(_CamelModel):
    name: str
    slug: str
    plan: str


class SyncState(_CamelModel):
    status: Literal["healthy", "syncing", "pending", "error"]
    label: str
    last_synced_at: datetime | None


class Kpi(_CamelModel):
    id: Literal["decisions", "policies", "skills", "reviews"]
    label: str
    value: int
    trend: int  # percentage delta vs the prior period (negative = down)
    spark: list[int]  # last 7 cumulative data points, oldest first


class OwnerSummary(_CamelModel):
    name: str | None
    avatar_color: str | None


class DecisionSummary(_CamelModel):
    id: str
    title: str
    source_provider: str | None
    source_location: str | None
    status: str
    confidence: int | None
    category: str | None
    owner: OwnerSummary | None
    monthly_uses: int
    updated_at: datetime
    summary: str | None
    rule: str | None


class ReviewSummary(_CamelModel):
    id: str
    title: str
    kind: str
    source_provider: str | None
    source_location: str | None
    before: str | None
    after: str | None
    evidence_quote: str | None
    evidence_author: str | None
    confidence: int | None
    status: str


class SourceSummary(_CamelModel):
    id: str
    provider: str
    name: str
    status: str
    sync_status: str
    last_synced_at: datetime | None
    health: int | None
    pending_items: int
    active_channel_count: int
    extracted_label: str


class ActivityEvent(_CamelModel):
    id: str
    type: str
    title: str
    detail: str | None
    source_provider: str | None
    created_at: datetime


# ── top-level responses ─────────────────────────────────────────────────────
class OverviewResponse(_CamelModel):
    workspace: WorkspaceSummary
    greeting_name: str
    sync: SyncState
    kpis: list[Kpi]
    recent_questions: list[str]
    review_preview: list[ReviewSummary]
    recent_decisions: list[DecisionSummary]
    source_health: list[SourceSummary]
    activity: list[ActivityEvent]


class UsageResponse(_CamelModel):
    """Real usage counters for the settings Usage tab + sidebar meter.

    Everything here is measured, never estimated: ``queries30d`` counts
    ``agent_interactions`` rows (dashboard brain chat + agent ``query_brain``),
    ``skillsServed30d`` counts the subset that matched a skill, ``activeSkills``
    counts published skills. ``querySeries`` is queries/day for the last 7 days,
    oldest first. There is no quota system yet, so no limit/percent fields —
    the FE must not fabricate one.
    """

    queries30d: int = Field(alias="queries30d")  # to_camel would mangle to queries30D
    skills_served_30d: int = Field(alias="skillsServed30d")
    active_skills: int
    query_series: list[int]
