from __future__ import annotations
from typing import Any
from uuid import UUID
from datetime import datetime
from pydantic import BaseModel


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


class GitHubSyncRequest(BaseModel):
    """POST body for /ingest/github/sync.

    All fields are optional; if `repos` is omitted we fall back to the
    GITHUB_REPOS env var.
    """
    repos: list[str] | None = None
    state: str = "all"  # 'open' | 'closed' | 'all'
    since: str | None = None  # ISO-8601, forwarded to GitHub on supported streams


class GitHubStreamCounts(BaseModel):
    fetched: int
    inserted: int
    updated: int


class GitHubRepoSyncResult(BaseModel):
    repo: str
    streams: dict[str, GitHubStreamCounts]
    errors: list[str] = []


class GitHubSyncResponse(BaseModel):
    repos: list[GitHubRepoSyncResult]


class ReviewAction(BaseModel):
    reviewer_id: str | None = None
    reason: str | None = None
