"""Decisions registry schemas (BACKEND_ASKS §8).

Read-only projection of the existing ``decisions`` table for the dashboard's
Decisions page. Responses serialize camelCase. A "decision" is a distinct domain
object from a skill (its own table, owner, category, and executable ``rule``).
"""
from __future__ import annotations

from datetime import datetime

from pydantic import Field

from app.shared.schemas import CamelModel


class DecisionOwner(CamelModel):
    name: str | None = None
    avatar_color: str | None = None


class DecisionOut(CamelModel):
    """One decision row/detail (list and get share this shape)."""

    id: str
    title: str
    provider: str | None = None  # source_provider
    location: str | None = None  # source_location
    status: str
    confidence: int | None = None
    category: str | None = None
    owner: DecisionOwner | None = None
    uses: int = 0  # monthly_uses
    updated_at: datetime | None = None
    body: str | None = None  # summary
    rule: str | None = None
