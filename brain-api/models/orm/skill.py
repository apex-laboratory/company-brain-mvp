import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class Skill(Base):
    __tablename__ = "skills"
    __table_args__ = (
        UniqueConstraint("org_id", "name", name="skills_org_id_name_key"),
        Index("ix_skills_org_id_status", "org_id", "status"),
        Index("ix_skills_org_id_source_authority", "org_id", "source_authority"),
        # HNSW index (skills_embedding_hnsw) is raw SQL — declared in migration only
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    trigger: Mapped[str | None] = mapped_column(String)
    base_logic: Mapped[str | None] = mapped_column(String)
    exceptions_block: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'"))
    actions: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'"))
    source_ids: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'"))
    source_authority: Mapped[str | None] = mapped_column(String(10))
    conflict_flags: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'"))
    status: Mapped[str] = mapped_column(String(20), server_default=text("'draft'"))
    confidence: Mapped[float | None] = mapped_column(Float)
    embedding: Mapped[list | None] = mapped_column(Vector(1536))
    changed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("NOW()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("NOW()"))


class SkillVersion(Base):
    __tablename__ = "skill_versions"
    __table_args__ = (Index("ix_skill_versions_org_id_skill_id", "org_id", "skill_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    skill_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("skills.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    base_logic: Mapped[str | None] = mapped_column(String)
    exceptions_block: Mapped[dict | None] = mapped_column(JSONB)
    confidence: Mapped[float | None] = mapped_column(Float)
    changed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    change_type: Mapped[str | None] = mapped_column(String(30))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("NOW()"))
