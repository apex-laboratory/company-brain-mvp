"""Source-integration request/response schemas (BACKEND_BEST_PRACTICES.md §3, §5).

Requests reject unknown keys; responses serialize to camelCase per the API
contract (API_DOCUMENTATION.md §Source Integrations API). Provider tokens never
appear in any response schema — only connection metadata.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field

from app.shared.schemas import CamelModel as _Response
from app.shared.schemas import CamelRequestModel as _Request

SourceProvider = Literal["slack", "notion", "github", "jira", "zendesk", "google_drive"]
# Sweep/ingestion lookback window (API_DOCUMENTATION.md §Configure Source Scope).
TimeRange = Literal["30d", "90d", "6mo", "all"]


# ── requests ──────────────────────────────────────────────────────────────────
class SourceConnectRequest(_Request):
    redirect_uri: Annotated[str, Field(min_length=1, max_length=2048)]
    # Optional scope override; defaults to the provider's read-only scopes.
    requested_scopes: list[str] | None = None


class SourceCallbackRequest(_Request):
    code: Annotated[str, Field(min_length=1, max_length=4096)]
    state: Annotated[str, Field(min_length=1, max_length=4096)]


class SourceScopeRequest(_Request):
    time_range: TimeRange
    # provider -> list of channel/space/project external ids to select.
    channels: dict[SourceProvider, list[str]]


# ── responses ─────────────────────────────────────────────────────────────────
class ProviderOut(_Response):
    provider: SourceProvider
    name: str
    tag: str
    default_scopes: list[str]
    read_only: bool
    # A pre-connect volume hint (e.g. "3,412 messages"); unknown until estimated.
    estimated_items: str | None = None


class SourceOut(_Response):
    id: str
    provider: SourceProvider
    name: str
    status: str
    sync_status: str
    last_synced_at: datetime | None
    health: int | None
    active_channel_count: int
    # Sync-derived fields (API_DOCUMENTATION.md §List Connected Sources). Populated
    # by the ingestion job; reported as empty/zero until the first sync runs.
    pending_items: int = 0
    extracted_label: str | None = None
    ingest7d: list[int] = Field(default_factory=list)


class SourceConnectStartOut(_Response):
    authorization_url: str
    state: str


class SourceChannelOut(_Response):
    id: str
    name: str
    provider: SourceProvider
    selected: bool
    item_count: int


class SourceScopeUpdatedOut(_Response):
    status: Literal["configured"]
    estimated_decisions: int
