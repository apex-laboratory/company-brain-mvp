from datetime import date, datetime

from sqlalchemy import ARRAY, BigInteger, Date, DateTime, ForeignKey, Index, Integer, LargeBinary, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class ApiKey(Base):
    """
    Workspace API keys for MCP / agent / LangGraph access.
    key_hash is a usable credential — admins only via RLS.
    Pre-tenant lookup (authenticate by key_hash) runs through a SECURITY DEFINER
    function that bypasses RLS before workspace context exists.
    """
    __tablename__ = "api_keys"
    __table_args__ = (
        Index("ix_api_keys_workspace_id", "workspace_id"),
        Index("ix_api_keys_key_hash",     "key_hash"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)              # key_…
    workspace_id: Mapped[str] = mapped_column(
        Text, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)              # 'Production MCP client'
    key_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_prefix: Mapped[str] = mapped_column(Text, nullable=False)        # 'hph_live_abc1' for UI identification
    scopes: Mapped[list] = mapped_column(ARRAY(Text), nullable=False, server_default=text("'{}'"))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(Text, ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class UsagePeriod(Base):
    """
    Metering rows upserted by the usage.rollup job.
    spark JSONB: precomputed sparkline arrays for the billing page UI.
    """
    __tablename__ = "usage_periods"
    __table_args__ = (
        UniqueConstraint("workspace_id", "period_start",
                         name="usage_periods_workspace_id_period_start_key"),
        # DESC index declared in migration only
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        Text, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    brain_queries: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    brain_query_limit: Mapped[int | None] = mapped_column(Integer)
    mcp_calls: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    skills_served: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    tokens_used: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    spark: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
