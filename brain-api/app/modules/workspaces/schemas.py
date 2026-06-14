"""Workspace request/response schemas (BACKEND_BEST_PRACTICES.md §3, §5, §14).

Requests reject unknown keys (extra="forbid"); responses serialize to camelCase
per the API contract.
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.shared.schemas import CamelModel as _CamelModel


class _Request(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        alias_generator=lambda s: "".join(
            w.capitalize() if i else w for i, w in enumerate(s.split("_"))
        ),
        populate_by_name=True,
    )


_TeamSize = Literal["1-10", "11-50", "51-200", "200+"]
_UseCase = Literal["support", "ops", "eng", "agents"]
_OnboardingStep = Literal["company", "connect", "configure", "build", "done"]
_TimeRange = Literal["30d", "90d", "6mo", "all"]


# ── creation + onboarding requests ────────────────────────────────────────────

class CreateWorkspaceRequest(_Request):
    company_name: str = Field(..., min_length=1, max_length=100)
    team_size: _TeamSize
    primary_use_case: _UseCase


class OnboardingPatchRequest(_Request):
    step: _OnboardingStep
    company_name: str | None = Field(default=None, max_length=100)
    team_size: _TeamSize | None = None
    primary_use_case: _UseCase | None = None
    connected_providers: list[str] | None = None
    time_range: _TimeRange | None = None
    channels: dict[str, list[str]] | None = None


# ── settings update request ────────────────────────────────────────────────────

class WorkspaceSettingsUpdate(BaseModel):
    """Partial update of workspace settings (PATCH semantics)."""

    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    domain: Annotated[str, Field(max_length=255)] | None = None

    @field_validator("name", mode="after")
    @classmethod
    def _reject_null_name(cls, v: str | None) -> str | None:
        if v is None:
            raise ValueError("name cannot be null")
        return v

    @field_validator("domain", mode="after")
    @classmethod
    def _blank_domain_is_null(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = v.strip()
        return stripped or None


# ── responses ─────────────────────────────────────────────────────────────────

class WorkspaceOut(_CamelModel):
    id: str
    name: str
    slug: str
    plan: str


class CreateWorkspaceOut(_CamelModel):
    workspace: WorkspaceOut
    access_token: str


class OnboardingOut(_CamelModel):
    status: str
    next_step: str


class WorkspaceConfig(_CamelModel):
    name: str
    domain: str | None
    plan: str
    seat_limit: int


class SettingsResponse(_CamelModel):
    """GET /settings payload: workspace config + the per-workspace MCP URL."""

    workspace: WorkspaceConfig
    brain_endpoint: str


class UpdatedWorkspace(_CamelModel):
    id: str
    name: str
    domain: str | None


class UpdateSettingsResponse(_CamelModel):
    """PATCH /settings payload."""

    workspace: UpdatedWorkspace
