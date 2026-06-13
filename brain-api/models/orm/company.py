from datetime import datetime

from sqlalchemy import DateTime, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class Company(Base):
    """
    Global marketing / design-partner waitlist. Pre-dates any workspace,
    so there is no workspace_id and RLS is NOT enabled.
    Written by the public POST /api/companies route (no auth required).
    """
    __tablename__ = "companies"

    id: Mapped[str] = mapped_column(Text, primary_key=True)              # cmp_…
    contact_name: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    company: Mapped[str] = mapped_column(Text, nullable=False)
    company_size: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
