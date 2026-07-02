from datetime import datetime

from sqlalchemy import ARRAY, DateTime, Enum, ForeignKey, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .enums import source_provider_enum as _source_provider

_build_status = Enum("queued", "running", "completed", "failed", "canceled",
                      name="build_status", create_type=False)
_message_role = Enum("user", "assistant", name="message_role", create_type=False)


class BrainBuild(Base):
    """
    Status mirror for AI-service extraction jobs.
    Backend creates the row (queued), an ARQ job calls the AI service,
    and the row is updated as progress arrives. ai_job_id links to the AI-service job.
    """
    __tablename__ = "brain_builds"
    __table_args__ = (Index("ix_brain_builds_workspace_id_status", "workspace_id", "status"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True)              # bld_…
    workspace_id: Mapped[str] = mapped_column(
        Text, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(_build_status, nullable=False, server_default=text("'queued'"))
    progress: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))  # 0–100
    current_step: Mapped[str | None] = mapped_column(Text)
    time_range: Mapped[str | None] = mapped_column(Text)                 # '30d'|'90d'|'6mo'|'all'
    source_ids: Mapped[list] = mapped_column(ARRAY(Text), nullable=False, server_default=text("'{}'"))
    extract: Mapped[list] = mapped_column(ARRAY(Text), nullable=False, server_default=text("'{}'"))
    counts: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'"))
    ai_job_id: Mapped[str | None] = mapped_column(Text)
    triggered_by: Mapped[str | None] = mapped_column(Text, ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class BrainConversation(Base):
    __tablename__ = "brain_conversations"

    id: Mapped[str] = mapped_column(Text, primary_key=True)              # cnv_…
    workspace_id: Mapped[str] = mapped_column(
        Text, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str | None] = mapped_column(Text, ForeignKey("users.id", ondelete="SET NULL"))
    title: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class BrainMessage(Base):
    """
    Append-only conversation messages. No UPDATE policy in RLS;
    both user and assistant turns are inserted by the backend.
    sources JSONB: [{provider, label, sourceItemId, url, excerpt}]
    """
    __tablename__ = "brain_messages"
    __table_args__ = (
        Index("ix_brain_messages_workspace_id_conversation_id_created_at",
              "workspace_id", "conversation_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)              # msg_…
    workspace_id: Mapped[str] = mapped_column(
        Text, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[str] = mapped_column(
        Text, ForeignKey("brain_conversations.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(_message_role, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[int | None] = mapped_column(Integer)              # 0–100; assistant turns only
    sources: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class ActivityEvent(Base):
    """Lightweight dashboard feed. Append-only; system inserts, members read."""
    __tablename__ = "activity_events"

    id: Mapped[str] = mapped_column(Text, primary_key=True)              # evt_…
    workspace_id: Mapped[str] = mapped_column(
        Text, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[str] = mapped_column(Text, nullable=False)              # 'skill'|'decision'|'review'|'source'…
    title: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    source_provider: Mapped[str | None] = mapped_column(_source_provider)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
