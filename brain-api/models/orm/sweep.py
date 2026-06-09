import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class Sweep(Base):
    """AI-internal: onboarding + manual extraction jobs. UUID PK."""
    __tablename__ = "sweeps"
    __table_args__ = (Index("ix_sweeps_workspace_id_status", "workspace_id", "status"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    workspace_id: Mapped[str] = mapped_column(
        Text, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'running'"))
    config: Mapped[dict | None] = mapped_column(JSONB)                   # source_authority config at sweep time
    progress: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'"))
    skills_created: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    skills_queued: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    triggered_by: Mapped[str | None] = mapped_column(Text, ForeignKey("users.id"))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
