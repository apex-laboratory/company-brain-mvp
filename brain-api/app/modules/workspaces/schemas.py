"""Workspace settings request/response schemas (BEST_PRACTICES §3, §5, §14).

Responses serialize to camelCase per the API contract (API_DOCUMENTATION.md
§Settings And API Keys). The update request rejects unknown keys
(``extra="forbid"``) and applies only the fields the caller actually sent.
"""
from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.shared.schemas import CamelModel as _CamelModel


# ── requests ──────────────────────────────────────────────────────────────────
class WorkspaceSettingsUpdate(BaseModel):
    """Partial update of workspace settings.

    Both fields are optional (PATCH semantics): only those present in the request
    are written. ``domain`` may be set to ``null`` (or an empty/blank string,
    normalized below) to clear it; ``name`` is ``NOT NULL`` in the schema, so an
    explicit ``null`` is rejected (422).
    """

    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    domain: Annotated[str, Field(max_length=255)] | None = None

    @field_validator("name", mode="after")
    @classmethod
    def _reject_null_name(cls, v: str | None) -> str | None:
        # Field validators don't run for an omitted field (its default is used),
        # so reaching here with ``None`` means the client sent ``name: null``
        # explicitly — which the NOT NULL column forbids.
        if v is None:
            raise ValueError("name cannot be null")
        return v

    @field_validator("domain", mode="after")
    @classmethod
    def _blank_domain_is_null(cls, v: str | None) -> str | None:
        # Treat an empty/whitespace domain as a clear (NULL), so "" and null are
        # equivalent and the column never stores a meaningless empty string.
        if v is None:
            return None
        stripped = v.strip()
        return stripped or None


# ── responses ─────────────────────────────────────────────────────────────────
class WorkspaceConfig(_CamelModel):
    name: str
    domain: str | None
    plan: str
    seat_limit: int


class SettingsResponse(_CamelModel):
    """``GET /settings`` payload: workspace config + the per-workspace MCP URL."""

    workspace: WorkspaceConfig
    brain_endpoint: str


class UpdatedWorkspace(_CamelModel):
    id: str
    name: str
    domain: str | None


class UpdateSettingsResponse(_CamelModel):
    """``PATCH /settings`` payload."""

    workspace: UpdatedWorkspace
