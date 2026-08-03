import uuid
from datetime import datetime

from sqlalchemy import ARRAY, Boolean, CheckConstraint, DateTime, Enum, ForeignKey, Index, Integer, LargeBinary, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .enums import source_provider_enum as _source_provider

_source_status = Enum("connected", "disconnected", "error", "pending",
                       name="source_status", create_type=False)
_sync_status = Enum("healthy", "pending", "syncing", "error",
                     name="sync_status", create_type=False)


class SourceConnection(Base):
    __tablename__ = "source_connections"
    __table_args__ = (
        UniqueConstraint("workspace_id", "provider", "external_account_id",
                         name="source_connections_workspace_id_provider_account_key"),
        Index("ix_source_connections_workspace_id_provider_status",
              "workspace_id", "provider", "status"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)              # src_…
    workspace_id: Mapped[str] = mapped_column(
        Text, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(_source_provider, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(_source_status, nullable=False, server_default=text("'pending'"))
    sync_status: Mapped[str] = mapped_column(_sync_status, nullable=False, server_default=text("'pending'"))
    access_token_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    refresh_token_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scopes: Mapped[list] = mapped_column(ARRAY(Text), nullable=False, server_default=text("'{}'"))
    external_account_id: Mapped[str | None] = mapped_column(Text)
    sync_cursor: Mapped[str | None] = mapped_column(Text)                 # opaque incremental cursor (Drive pageToken / Gmail historyId)
    lookback_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("90"))
    health: Mapped[int | None] = mapped_column(Integer)                  # 0–100
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    backfilled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # historical import completed (NULL = never imported)
    connected_by: Mapped[str | None] = mapped_column(Text, ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class SourceChannel(Base):
    """
    One row per channel/space/project the workspace has selected for ingestion.
    Replaces the old monitored_ids JSONB blob on source_connections.
    """
    __tablename__ = "source_channels"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id",
                         name="source_channels_source_id_external_id_key"),
        Index("ix_source_channels_workspace_id_source_id", "workspace_id", "source_id"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)              # chn_…
    workspace_id: Mapped[str] = mapped_column(
        Text, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    source_id: Mapped[str] = mapped_column(
        Text, ForeignKey("source_connections.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(_source_provider, nullable=False)
    external_id: Mapped[str | None] = mapped_column(Text)                # provider-assigned channel/space/project id
    name: Mapped[str] = mapped_column(Text, nullable=False)              # '#cs-escalations'
    selected: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("FALSE"))
    item_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class WebhookSubscription(Base):
    __tablename__ = "webhook_subscriptions"
    __table_args__ = (
        Index("ix_webhook_subscriptions_workspace_id_provider_status",
              "workspace_id", "provider", "status"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)              # whs_…
    workspace_id: Mapped[str] = mapped_column(
        Text, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(_source_provider, nullable=False)
    source_ref_id: Mapped[str | None] = mapped_column(Text)              # subscription id assigned by provider
    target_id: Mapped[str | None] = mapped_column(Text)                  # channel/space/project monitored
    secret_enc: Mapped[bytes | None] = mapped_column(LargeBinary)        # encrypted signing secret / channel token
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # push-channel expiry (Google watch <= 7d)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class SourceEvent(Base):
    """AI-internal: webhook + sweep audit trail. UUID PK; not user-facing."""
    __tablename__ = "source_events"
    __table_args__ = (
        UniqueConstraint("workspace_id", "provider", "external_event_id",
                         name="source_events_workspace_provider_event_key"),
        Index("ix_source_events_workspace_id_provider_processed",
              "workspace_id", "provider", "processed"),
        Index("ix_source_events_workspace_id_sweep_id", "workspace_id", "sweep_id"),
        # Partial index for the dead-letter listing (0013):
        Index("ix_source_events_workspace_failed", "workspace_id",
              postgresql_where=text("outcome = 'failed'")),
        # Pin the outcome vocabulary (0016); mirrors pipeline.types.ALL_OUTCOMES.
        CheckConstraint(
            "outcome IS NULL OR outcome IN ('queued', 'published', 'review', 'draft', "
            "'discarded', 'duplicate', 'contradiction', 'failed')",
            name="source_events_outcome_check",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    workspace_id: Mapped[str] = mapped_column(
        Text, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(_source_provider, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    source_id: Mapped[str | None] = mapped_column(Text)
    external_event_id: Mapped[str | None] = mapped_column(Text)          # for deduplication
    # Connection that ingested this event; the expander resolves its token from it
    # (SET NULL on connection delete → falls back to first-connection-per-provider).
    source_connection_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("source_connections.id", ondelete="SET NULL")
    )
    payload: Mapped[dict | None] = mapped_column(JSONB)
    processed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("FALSE"))
    skill_id: Mapped[str | None] = mapped_column(Text, ForeignKey("skills.id", ondelete="SET NULL"))
    # queued → published|review|draft|discarded|duplicate|contradiction|failed.
    # processed=true on every terminal outcome incl. failed; re-run resets to
    # processed=false, outcome='queued'.
    outcome: Mapped[str | None] = mapped_column(Text)
    # Pipeline dead-letter bookkeeping (0013): full-pipeline attempts (each one
    # already retried transient LLM failures internally) + per-stage costs/errors.
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    pipeline_meta: Mapped[dict | None] = mapped_column(JSONB)
    sweep_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
