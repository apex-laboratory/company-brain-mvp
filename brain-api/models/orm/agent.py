import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class AgentInteraction(Base):
    """AI-internal: episodic query log. UUID PK; written by the extraction pipeline."""
    __tablename__ = "agent_interactions"
    __table_args__ = (
        Index("ix_agent_interactions_workspace_id_user_id",  "workspace_id", "user_id"),
        Index("ix_agent_interactions_workspace_id_skill_id", "workspace_id", "skill_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    workspace_id: Mapped[str] = mapped_column(
        Text, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str | None] = mapped_column(Text, ForeignKey("users.id"))
    skill_id: Mapped[str | None] = mapped_column(Text, ForeignKey("skills.id", ondelete="SET NULL"))
    query: Mapped[str | None] = mapped_column(Text)
    matched_confidence: Mapped[float | None] = mapped_column(Float)
    match_type: Mapped[str | None] = mapped_column(Text)                 # semantic|query_driven|no_match
    agent_action: Mapped[dict | None] = mapped_column(JSONB)
    human_override: Mapped[bool] = mapped_column(Boolean, server_default=text("FALSE"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
