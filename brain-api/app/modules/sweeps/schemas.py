"""Sweeps request/response schemas (BACKEND_BEST_PRACTICES.md §3, §5).

Responses serialize to camelCase. ``progress`` is the per-provider map the
onboarding "Building your brain..." screen renders:
``{provider: {status: running|completed|failed, inserted: int, error?: str}}``.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class _Response(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class SweepOut(_Response):
    id: str
    status: str
    progress: dict
    skills_created: int = 0
    skills_queued: int = 0
    started_at: datetime
    completed_at: datetime | None = None
