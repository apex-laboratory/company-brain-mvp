import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Index, Integer, PrimaryKeyConstraint, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

_decision_status = Enum("approved", "active", "review",
                         name="decision_status", create_type=False)
_review_kind = Enum("policy_change", "new_decision", "contradiction", "exception",
                     name="review_kind", create_type=False)
_review_status = Enum("pending", "approved", "rejected",
                       name="review_status", create_type=False)
_source_provider = Enum("slack", "notion", "github", "jira", "zendesk",
                         name="source_provider", create_type=False)


class Decision(Base):
    """
    Projection of AI-service decisions. ai_decision_id links back to the UUID in
    the AI service. Soft-deleted (deleted_at); all reads filter deleted_at IS NULL.
    """
    __tablename__ = "decisions"
    __table_args__ = (
        Index("ix_decisions_workspace_id_status",          "workspace_id", "status"),
        Index("ix_decisions_workspace_id_category",        "workspace_id", "category"),
        Index("ix_decisions_workspace_id_source_provider", "workspace_id", "source_provider"),
        # GIN full-text (decisions_search) declared in migration only
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)              # dec_…
    workspace_id: Mapped[str] = mapped_column(
        Text, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    ai_decision_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    title: Mapped[str] = mapped_column(Text, nullable=False)
    source_provider: Mapped[str | None] = mapped_column(_source_provider)
    source_location: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(_decision_status, nullable=False, server_default=text("'review'"))
    confidence: Mapped[int | None] = mapped_column(Integer)              # 0–100
    category: Mapped[str | None] = mapped_column(Text)
    owner_user_id: Mapped[str | None] = mapped_column(Text, ForeignKey("users.id", ondelete="SET NULL"))
    monthly_uses: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    summary: Mapped[str | None] = mapped_column(Text)
    rule: Mapped[str | None] = mapped_column(Text)
    provenance: Mapped[dict | None] = mapped_column(JSONB)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class DecisionPin(Base):
    """User-pinned decisions. At most one pin per (decision, user) pair."""
    __tablename__ = "decision_pins"
    __table_args__ = (
        PrimaryKeyConstraint("decision_id", "user_id", name="decision_pins_pkey"),
    )

    workspace_id: Mapped[str] = mapped_column(
        Text, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    decision_id: Mapped[str] = mapped_column(
        Text, ForeignKey("decisions.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class Review(Base):
    """
    Human-in-the-loop review queue. Replaces the old review_queue table.
    ai_review_id links to the AI service's queue record.
    On approve, decision_id is set to the resulting decision row.
    """
    __tablename__ = "reviews"
    __table_args__ = (Index("ix_reviews_workspace_id_status", "workspace_id", "status"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True)              # rev_…
    workspace_id: Mapped[str] = mapped_column(
        Text, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    ai_review_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    title: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(_review_kind, nullable=False)
    source_provider: Mapped[str | None] = mapped_column(_source_provider)
    source_location: Mapped[str | None] = mapped_column(Text)
    before_text: Mapped[str | None] = mapped_column(Text)
    after_text: Mapped[str | None] = mapped_column(Text)
    evidence_quote: Mapped[str | None] = mapped_column(Text)
    evidence_author: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[int | None] = mapped_column(Integer)              # 0–100
    status: Mapped[str] = mapped_column(_review_status, nullable=False, server_default=text("'pending'"))
    verdict: Mapped[str | None] = mapped_column(Text)                    # 'approve'|'reject'
    comment: Mapped[str | None] = mapped_column(Text)
    resolved_by: Mapped[str | None] = mapped_column(Text, ForeignKey("users.id", ondelete="SET NULL"))
    decision_id: Mapped[str | None] = mapped_column(Text, ForeignKey("decisions.id", ondelete="SET NULL"))
    skill_id: Mapped[str | None] = mapped_column(Text, ForeignKey("skills.id", ondelete="SET NULL"))
    merged_into_brain: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("FALSE"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
