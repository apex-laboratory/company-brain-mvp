import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class AgentInteraction(Base):
    __tablename__ = "agent_interactions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    skill_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("skills.id", ondelete="SET NULL")
    )
    query: Mapped[str | None] = mapped_column(String)
    matched_confidence: Mapped[float | None] = mapped_column(Float)
    match_type: Mapped[str | None] = mapped_column(String(20))
    agent_action: Mapped[dict | None] = mapped_column(JSONB)
    human_override: Mapped[bool] = mapped_column(Boolean, server_default=text("FALSE"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("NOW()"))
