import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class ReviewQueue(Base):
    __tablename__ = "review_queue"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    skill_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("skills.id", ondelete="SET NULL")
    )
    review_type: Mapped[str] = mapped_column(String(30), nullable=False)
    proposed_update: Mapped[dict | None] = mapped_column(JSONB)
    source_a: Mapped[dict | None] = mapped_column(JSONB)
    source_b: Mapped[dict | None] = mapped_column(JSONB)
    confidence: Mapped[float | None] = mapped_column(Float)
    reason: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String(20), server_default=text("'pending'"))
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("NOW()"))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)
