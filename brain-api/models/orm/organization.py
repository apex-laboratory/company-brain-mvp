import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, LargeBinary, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

member_role_enum = Enum(
    "owner", "admin", "editor", "viewer",
    name="member_role",
)


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    plan: Mapped[str] = mapped_column(String(20), server_default=text("'trial'"))
    plan_seats: Mapped[int] = mapped_column(Integer, server_default=text("5"))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("TRUE"))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("NOW()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("NOW()"))


class OrganizationMember(Base):
    __tablename__ = "organization_members"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(member_role_enum, nullable=False, server_default=text("'viewer'"))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("TRUE"))
    invited_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    joined_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("NOW()"))


class Invitation(Base):
    __tablename__ = "invitations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(member_role_enum, nullable=False, server_default=text("'viewer'"))
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, unique=True)
    invited_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=text("NOW() + INTERVAL '7 days'")
    )
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("NOW()"))


class OrganizationSettings(Base):
    __tablename__ = "organization_settings"

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    max_seats: Mapped[int] = mapped_column(Integer, server_default=text("5"))
    max_skills: Mapped[int] = mapped_column(Integer, server_default=text("500"))
    max_sweeps_per_day: Mapped[int] = mapped_column(Integer, server_default=text("3"))
    retention_days: Mapped[int] = mapped_column(Integer, server_default=text("365"))
    sso_enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("FALSE"))
    sso_provider: Mapped[str | None] = mapped_column(String(20))
    branding: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'"))
    sso_config: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'"))
    features: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'"))
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("NOW()"))
