from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Index, Integer, LargeBinary, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

_member_role = Enum("admin", "editor", "viewer", name="member_role", create_type=False)
_invite_status = Enum("pending", "accepted", "revoked", "expired", name="invite_status", create_type=False)
_workspace_plan = Enum("trial", "starter", "pro", "enterprise", name="workspace_plan", create_type=False)


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(Text, primary_key=True)              # wrk_…
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    domain: Mapped[str | None] = mapped_column(Text)
    plan: Mapped[str] = mapped_column(_workspace_plan, nullable=False, server_default=text("'trial'"))
    seat_limit: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("5"))
    team_size: Mapped[str | None] = mapped_column(Text)                  # '1-10'|'11-50'|…
    primary_use_case: Mapped[str | None] = mapped_column(Text)           # 'support'|'ops'|…
    onboarding_step: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(Text, ForeignKey("users.id"))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class WorkspaceMember(Base):
    __tablename__ = "workspace_members"
    __table_args__ = (
        UniqueConstraint("workspace_id", "user_id", name="workspace_members_workspace_id_user_id_key"),
        Index("ix_workspace_members_workspace_id_user_id", "workspace_id", "user_id"),
        Index("ix_workspace_members_user_id", "user_id"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)              # mem_…
    workspace_id: Mapped[str] = mapped_column(
        Text, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(_member_role, nullable=False, server_default=text("'viewer'"))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("TRUE"))
    invited_by: Mapped[str | None] = mapped_column(Text, ForeignKey("users.id"))
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class Invitation(Base):
    __tablename__ = "invitations"
    __table_args__ = (Index("ix_invitations_workspace_id_email", "workspace_id", "email"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True)              # inv_…
    workspace_id: Mapped[str] = mapped_column(
        Text, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    email: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(_member_role, nullable=False, server_default=text("'viewer'"))
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, unique=True)
    status: Mapped[str] = mapped_column(_invite_status, nullable=False, server_default=text("'pending'"))
    invited_by: Mapped[str] = mapped_column(Text, ForeignKey("users.id"), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        server_default=text("now() + INTERVAL '7 days'")
    )
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class WorkspaceSettings(Base):
    __tablename__ = "workspace_settings"

    workspace_id: Mapped[str] = mapped_column(
        Text, ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    max_seats: Mapped[int] = mapped_column(Integer, server_default=text("5"))
    max_skills: Mapped[int] = mapped_column(Integer, server_default=text("500"))
    max_sweeps_per_day: Mapped[int] = mapped_column(Integer, server_default=text("3"))
    retention_days: Mapped[int] = mapped_column(Integer, server_default=text("365"))
    sso_enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("FALSE"))
    sso_provider: Mapped[str | None] = mapped_column(Text)               # 'saml'|'oidc'
    branding: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'"))
    sso_config: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'"))
    features: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
