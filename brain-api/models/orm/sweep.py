import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class Sweep(Base):
    __tablename__ = "sweeps"
    __table_args__ = (Index("ix_sweeps_org_id_status", "org_id", "status"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), server_default=text("'running'"))
    config: Mapped[dict | None] = mapped_column(JSONB)
    progress: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'"))
    skills_created: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    skills_queued: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    triggered_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    started_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("NOW()"))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
