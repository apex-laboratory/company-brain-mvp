"""Sources request/response schemas (BACKEND_BEST_PRACTICES.md §3, §5).

Requests reject unknown keys; responses serialize to camelCase.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class _Request(BaseModel):
    # Accept the camelCase keys our responses emit (externalId, lookbackDays, …) while
    # still allowing snake_case; unknown keys are rejected.
    model_config = ConfigDict(
        extra="forbid", alias_generator=to_camel, populate_by_name=True
    )


class _Response(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


# ── responses ─────────────────────────────────────────────────────────────────
class AuthorizeStartOut(_Response):
    authorize_url: str


class SourceConnectionOut(_Response):
    id: str
    provider: str
    name: str
    status: str
    sync_status: str
    external_account_id: str | None = None
    last_synced_at: datetime | None = None
    health: int | None = None
    created_at: datetime


class ChannelOut(_Response):
    id: str | None = None  # null until persisted (discovered-but-unselected)
    external_id: str
    name: str
    selected: bool = False
    item_count: int = 0


# ── requests ──────────────────────────────────────────────────────────────────
class AuthorizeStartRequest(_Request):
    # Required for subdomain-scoped providers (e.g. Zendesk: 'acme' -> acme.zendesk.com);
    # omitted for global-endpoint providers (Notion, GitHub).
    subdomain: str | None = None


class ChannelSelection(_Request):
    external_id: str
    name: str
    selected: bool = True


class ChannelSelectRequest(_Request):
    channels: list[ChannelSelection]
    # Onboarding's "how far back?" selector. Bounds the connection's first sync
    # (source_connections.lookback_days); omitted = keep the current value.
    lookback_days: int | None = Field(default=None, ge=1, le=730)
