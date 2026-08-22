"""API key request/response schemas (BEST_PRACTICES §3, §5, §14).

Responses serialize to camelCase per the API contract (API_DOCUMENTATION.md
§Settings And API Keys). The raw key is returned **only** in the creation
response — list responses expose the ``prefix`` and never the raw or hashed key.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.shared.schemas import CamelModel as _CamelModel

# Allowed scopes for a workspace API key (API_DOCUMENTATION.md §Settings And API
# Keys). ``require_scope`` enforces these per-route on the agent/MCP surface.
# ``runs:write`` is write-only by design: a credential that can push agent run
# traces cannot read them back. That lets a CI harness or a teammate's hook shim
# feed the self-improving loop without also handing it read access to the brain.
ApiKeyScope = Literal[
    "brain:query", "skills:invoke", "sources:read", "decisions:read", "runs:write"
]
ALLOWED_SCOPES: frozenset[str] = frozenset(get_args(ApiKeyScope))


# ── requests ──────────────────────────────────────────────────────────────────
class ApiKeyCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(min_length=1, max_length=200)]
    # No ``max_length`` here: it would be checked against the *raw* list before
    # ``_dedupe`` runs, so a request with duplicate-but-valid scopes that dedupe
    # within range would be wrongly rejected. The ``ApiKeyScope`` Literal already
    # caps the distinct values; dedup enforces the effective ceiling.
    scopes: Annotated[list[ApiKeyScope], Field(min_length=1)]

    @field_validator("scopes", mode="after")
    @classmethod
    def _dedupe(cls, v: list[str]) -> list[str]:
        """Drop duplicate scopes while preserving the caller's order."""
        return list(dict.fromkeys(v))


# ── responses ─────────────────────────────────────────────────────────────────
class ApiKeySummary(_CamelModel):
    """A single key in the list response — never carries the raw or hashed key."""

    id: str
    name: str
    prefix: str
    scopes: list[str]
    created_at: datetime
    last_used_at: datetime | None


class ApiKeyCreated(_CamelModel):
    """Creation response. ``api_key`` (the raw secret) is shown exactly once."""

    id: str
    name: str
    api_key: str
    prefix: str
    scopes: list[str]
    created_at: datetime
