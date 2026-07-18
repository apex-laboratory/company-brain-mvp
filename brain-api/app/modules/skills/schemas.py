"""Skills delivery schemas (Phase 5 — PRD §14 API Surface, Feature 15/15a).

The agent-facing read surface: semantic search, full skill body, version history,
and the interaction-override feedback loop. Responses serialize camelCase via the
shared ``CamelModel``; requests reject unknown keys via ``CamelRequestModel``.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import Field

from app.shared.schemas import CamelModel, CamelRequestModel


class SkillSearchResult(CamelModel):
    """One semantic-search hit. ``similarity`` is cosine similarity (0–1)."""

    id: str
    name: str
    version: str
    base_logic: str
    exceptions_block: list = Field(default_factory=list)
    source_authority: str | None = None
    similarity: float


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


class OverrideRequest(CamelRequestModel):
    """Report that an agent overrode a matched skill (Feature 15a feedback loop)."""

    reason: str | None = None


class OverrideResult(CamelModel):
    interaction_id: str
    skill_id: str | None = None
    new_confidence: float | None = None
    review_created: bool = False
