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
    # When this connection's *history* was imported (migration 0022). NULL means it
    # never was — connecting a source ingests nothing on its own. Distinct from
    # last_synced_at, which is the incremental cursor and moves on every sync.
    backfilled_at: datetime | None = None
    # Computed in the repository: history never imported and nothing importing it now.
    # Drives the Sources page's "Import history" button.
    needs_backfill: bool = False
    # What this source has read and what became of it, over its whole lifetime
    # (not just the last import — webhooks add events outside any sweep). Zero
    # rather than null for a source that has read nothing yet.
    items_read: int = 0
    skills_kept: int = 0
    discarded: int = 0
    pending_items: int = 0
    health: int | None = None
    created_at: datetime


class DiscardGroupOut(_Response):
    """One reason-bucket in the read report, keyed by the pipeline stage."""

    stage: str
    label: str
    count: int
    sample_reasons: list[str] = []


class SourceReportOut(_Response):
    """`GET /sources/{id}/report` — what this source read and why things dropped."""

    source_id: str
    items_read: int
    skills_kept: int
    discarded: int
    pending_items: int
    discarded_by_stage: list[DiscardGroupOut] = []


class ChannelOut(_Response):
    id: str | None = None  # null until persisted (discovered-but-unselected)
    external_id: str
    name: str
    selected: bool = False
    item_count: int = 0


class SourceScopeOut(_Response):
    """Everything the scope picker edits, read and written as one unit.

    ``lookback_days`` rides along with the channels so the client can render the
    *saved* window instead of guessing — the column is NOT NULL (default 90), so
    this is always a concrete number, never "unknown".
    """

    channels: list[ChannelOut]
    lookback_days: int


# ── requests ──────────────────────────────────────────────────────────────────
class AuthorizeStartRequest(_Request):
    # Required for subdomain-scoped providers (e.g. Zendesk: 'acme' -> acme.zendesk.com);
    # omitted for global-endpoint providers (Notion, GitHub).
    subdomain: str | None = None
    # Frontend path the callback should redirect to (e.g. '/onboarding'). Checked
    # against a server-side allowlist; non-allowlisted values fall back to the default.
    return_to: str | None = None


class ChannelSelection(_Request):
    external_id: str
    name: str
    selected: bool = True


class ChannelSelectRequest(_Request):
    channels: list[ChannelSelection]
    # Onboarding's "how far back?" selector. Bounds the connection's first sync
    # (source_connections.lookback_days); omitted = keep the current value.
    lookback_days: int | None = Field(default=None, ge=1, le=730)
