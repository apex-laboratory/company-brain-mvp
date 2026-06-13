from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import ARRAY, DateTime, Enum, Float, ForeignKey, Index, Integer, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

_skill_status = Enum("stable", "active", "draft", "review", name="skill_status", create_type=False)


class Skill(Base):
    """
    Unified skills table: holds both API projection columns (for the dashboard)
    and AI-service columns (embedding, base_logic, extraction output).
    The repository layer selects only the relevant subset per caller.
    """
    __tablename__ = "skills"
    __table_args__ = (
        UniqueConstraint("workspace_id", "name", name="skills_workspace_id_name_key"),
        Index("ix_skills_workspace_id_status", "workspace_id", "status"),
        Index("ix_skills_workspace_id_source_authority", "workspace_id", "source_authority"),
        # HNSW (skills_embedding_hnsw) and GIN (skills_name_search) declared in migration only
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)              # skl_…
    workspace_id: Mapped[str] = mapped_column(
        Text, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )

    # ── API projection ─────────────────────────────────────────────────────────
    name: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'v1'"))
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(_skill_status, nullable=False, server_default=text("'draft'"))
    source_providers: Mapped[list] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )
    input_schema: Mapped[dict | None] = mapped_column(JSONB)
    output_schema: Mapped[dict | None] = mapped_column(JSONB)
    calls_30d: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    # ── AI service columns ─────────────────────────────────────────────────────
    trigger: Mapped[str | None] = mapped_column(Text)
    base_logic: Mapped[str | None] = mapped_column(Text)
    exceptions_block: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'"))
    actions: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'"))
    source_ids: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'"))
    source_authority: Mapped[str | None] = mapped_column(Text)           # 'high'|'medium'|'low'
    conflict_flags: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'"))
    confidence: Mapped[float | None] = mapped_column(Float)
    embedding: Mapped[list | None] = mapped_column(Vector(1536))

    # ── Shared ─────────────────────────────────────────────────────────────────
    changed_by: Mapped[str | None] = mapped_column(Text, ForeignKey("users.id"))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class SkillVersion(Base):
    __tablename__ = "skill_versions"
    __table_args__ = (Index("ix_skill_versions_workspace_id_skill_id", "workspace_id", "skill_id"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        Text, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    skill_id: Mapped[str] = mapped_column(
        Text, ForeignKey("skills.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[str] = mapped_column(Text, nullable=False)           # 'v1','v2'…
    description: Mapped[str | None] = mapped_column(Text)
    base_logic: Mapped[str | None] = mapped_column(Text)
    exceptions_block: Mapped[dict | None] = mapped_column(JSONB)
    input_schema: Mapped[dict | None] = mapped_column(JSONB)
    output_schema: Mapped[dict | None] = mapped_column(JSONB)
    confidence: Mapped[float | None] = mapped_column(Float)
    changed_by: Mapped[str | None] = mapped_column(Text, ForeignKey("users.id"))
    change_type: Mapped[str | None] = mapped_column(Text)                # create|update|human_edit|sweep_sourced
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
