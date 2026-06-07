import uuid
from datetime import date, datetime

from sqlalchemy import ARRAY, BigInteger, Date, DateTime, ForeignKey, Index, Integer, LargeBinary, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class OrganizationApiKey(Base):
    __tablename__ = "organization_api_keys"
    __table_args__ = (
        Index("ix_organization_api_keys_org_id", "org_id"),
        Index("ix_organization_api_keys_key_hash", "key_hash"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    key_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_prefix: Mapped[str] = mapped_column(String(10), nullable=False)
    scopes: Mapped[list] = mapped_column(ARRAY(String), server_default=text('\'{"read"}\''))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("NOW()"))


class OrganizationUsage(Base):
    __tablename__ = "organization_usage"
    __table_args__ = (
        UniqueConstraint("org_id", "period_start", name="organization_usage_org_id_period_start_key"),
        # ix_organization_usage_org_id_period_start is a DESC functional index — declared in migration only
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    skills_total: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    sweeps_run: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    queries_total: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    tokens_used: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    embeddings_run: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    connector_syncs: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("NOW()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("NOW()"))
