import uuid
from datetime import datetime

from sqlalchemy import ARRAY, Boolean, DateTime, ForeignKey, Index, Integer, LargeBinary, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class SourceConnection(Base):
    __tablename__ = "source_connections"
    __table_args__ = (
        UniqueConstraint("org_id", "source", "external_account_id", name="source_connections_org_id_source_external_account_id_key"),
        Index("ix_source_connections_org_id_source_status", "org_id", "source", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(20), server_default=text("'connected'"))
    access_token_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    refresh_token_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    scopes: Mapped[list | None] = mapped_column(ARRAY(String))
    external_account_id: Mapped[str | None] = mapped_column(String(255))
    monitored_ids: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'"))
    lookback_days: Mapped[int] = mapped_column(Integer, server_default=text("180"))
    connected_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    connected_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("NOW()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("NOW()"))


class WebhookSubscription(Base):
    __tablename__ = "webhook_subscriptions"
    __table_args__ = (Index("ix_webhook_subscriptions_org_id_source_status", "org_id", "source", "status"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    source_ref_id: Mapped[str | None] = mapped_column(String(255))
    target_id: Mapped[str | None] = mapped_column(String(255))
    secret_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    status: Mapped[str] = mapped_column(String(20), server_default=text("'active'"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("NOW()"))


class SourceEvent(Base):
    __tablename__ = "source_events"
    __table_args__ = (
        UniqueConstraint("org_id", "source", "external_event_id", name="source_events_org_id_source_external_event_id_key"),
        Index("ix_source_events_org_id_source_processed", "org_id", "source", "processed"),
        Index("ix_source_events_org_id_sweep_id", "org_id", "sweep_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    source_id: Mapped[str | None] = mapped_column(String(255))
    external_event_id: Mapped[str | None] = mapped_column(String(255))
    payload: Mapped[dict | None] = mapped_column(JSONB)
    processed: Mapped[bool] = mapped_column(Boolean, server_default=text("FALSE"))
    skill_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("skills.id", ondelete="SET NULL")
    )
    outcome: Mapped[str | None] = mapped_column(String(30))
    sweep_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("NOW()"))
