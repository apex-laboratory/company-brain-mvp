import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
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


class BrainChunk(Base):
    """AI-internal: the unified chat retrieval index (BRAIN_CHAT_RAG_PLAN P2/P3).

    One embedded chunk of a skill's knowledge. ``kind='skill_version'`` (Phase 2)
    holds current + historical version bodies (``is_current`` distinguishes the live
    rule from superseded ones); ``kind='evidence'`` (Phase 3) holds the source
    material that fed a skill (``source_ref`` = {provider, sourceItemId, url, label,
    author}). Every chunk is skill-linked and workspace-isolated by RLS.

    ``chunk_key`` is the idempotency natural key (unique per workspace): the backfill
    upserts on it so re-runs skip already-indexed chunks. System-written only.
    """
    __tablename__ = "brain_chunks"
    __table_args__ = (
        UniqueConstraint("workspace_id", "chunk_key",
                         name="brain_chunks_workspace_id_chunk_key_key"),
        Index("ix_brain_chunks_workspace_id_skill_id", "workspace_id", "skill_id"),
        Index("ix_brain_chunks_workspace_id_kind_is_current",
              "workspace_id", "kind", "is_current"),
        CheckConstraint("kind IN ('skill_version', 'evidence')",
                        name="brain_chunks_kind_check"),
        # HNSW cosine index (brain_chunks_embedding_hnsw) declared in migration only
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    workspace_id: Mapped[str] = mapped_column(
        Text, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)               # skill_version | evidence
    skill_id: Mapped[str] = mapped_column(
        Text, ForeignKey("skills.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[str | None] = mapped_column(Text)                     # skill_version kind
    is_current: Mapped[bool | None] = mapped_column(Boolean)              # true=live, false=superseded
    source_ref: Mapped[dict | None] = mapped_column(JSONB)               # evidence kind
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    chunk_key: Mapped[str] = mapped_column(Text, nullable=False)          # idempotency natural key
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list | None] = mapped_column(Vector(1536))
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
