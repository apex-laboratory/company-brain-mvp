from __future__ import annotations
from typing import Any
from uuid import UUID
from datetime import datetime
from pydantic import BaseModel


# ─────────────────────────────────────────────
# Skill schemas
# ─────────────────────────────────────────────

class SkillCreate(BaseModel):
    name: str
    trigger: str | None = None
    base_logic: str | None = None
    exceptions_block: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    source_ids: list[str] = []
    source_authority: str | None = None
    changed_by: str = "human_authored"


class SkillRead(BaseModel):
    id: UUID
    name: str
    version: int
    trigger: str | None
    base_logic: str | None
    exceptions_block: Any
    actions: Any
    source_ids: Any
    source_authority: str | None
    conflict_flags: Any
    status: str
    confidence: float | None
    changed_by: str | None
    created_at: datetime
    updated_at: datetime


class SkillUpdate(BaseModel):
    trigger: str | None = None
    base_logic: str | None = None
    exceptions_block: list[dict[str, Any]] | None = None
    actions: list[dict[str, Any]] | None = None
    status: str | None = None


# ─────────────────────────────────────────────
# Ingest schemas
# ─────────────────────────────────────────────

class IngestEvent(BaseModel):
    source: str
    event_type: str
    source_id: str
    payload: dict[str, Any] = {}


class SweepConfig(BaseModel):
    sources: list[str] = ["notion", "github", "jira", "slack", "zendesk"]
    lookback_days: int = 180


class SweepSourceProgress(BaseModel):
    total: int = 0
    processed: int = 0
    published: int = 0
    queued: int = 0
    discarded: int = 0


class SweepStatus(BaseModel):
    id: UUID
    status: str
    sources: dict[str, SweepSourceProgress] = {}
    skills_created: int
    skills_queued: int
    started_at: datetime
    completed_at: datetime | None


# ─────────────────────────────────────────────
# Review schemas
# ─────────────────────────────────────────────

class ReviewAction(BaseModel):
    reviewer_id: str | None = None
    reason: str | None = None


class ReviewWrite(BaseModel):
    reviewer_id: str
    trigger: str
    base_logic: str
    exceptions_block: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []


class BulkApproveRequest(BaseModel):
    item_ids: list[UUID]
    reviewer_id: str | None = None


# ─────────────────────────────────────────────
# MCP query_brain response
# ─────────────────────────────────────────────

class QueryBrainResponse(BaseModel):
    skill_name: str
    trigger: str
    base_logic: str
    exceptions: list[dict[str, Any]]
    actions: list[dict[str, Any]]
    confidence: float
    source_authority: str
    version: int
    match_type: str
    # semantic | query_driven | cache_hit
    similarity_score: float | None = None
