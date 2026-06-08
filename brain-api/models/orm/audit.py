from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Text, text
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class AuditLog(Base):
    """
    Append-only audit trail. RLS grants SELECT + INSERT only — no UPDATE or DELETE
    policy exists, making rows immutable for all application roles.
    resource_id is TEXT (not UUID) since all resource PKs are now prefixed text IDs.
    """
    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_log_workspace_id_resource_resource_id",
              "workspace_id", "resource", "resource_id"),
        # DESC functional index (audit_log_workspace_id_created_at_desc) declared in migration only
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        Text, ForeignKey("workspaces.id"), nullable=False
    )
    user_id: Mapped[str | None] = mapped_column(Text, ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(Text, nullable=False)            # 'skill.published'|'member.invited'|…
    resource: Mapped[str | None] = mapped_column(Text)
    resource_id: Mapped[str | None] = mapped_column(Text)                # TEXT: all PKs are now prefixed
    old_value: Mapped[dict | None] = mapped_column(JSONB)
    new_value: Mapped[dict | None] = mapped_column(JSONB)
    ip_address: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
