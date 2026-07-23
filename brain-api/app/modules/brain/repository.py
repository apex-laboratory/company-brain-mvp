"""Data access for the brain chat surface (the only place its SQL lives).

Every method runs inside the caller's ``run_in_tenant`` transaction, so RLS scopes
each query to the workspace. Vector search itself is not here — the service reuses
the tested ``PipelineRepository.similar_skills`` HNSW query; this repository serves
the readiness count (and, from Phase 1, the provenance dossier + chat persistence).
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.skills.repository import PUBLISHED_STATUSES


class BrainRepository:
    async def count_indexed_skills(self, session: AsyncSession) -> int:
        """How many published skills carry an embedding (are retrievable).

        The Phase-0 readiness signal: chat is only "ready" once at least one
        reviewed skill is in the vector index. RLS scopes the count to the
        workspace; ``deleted_at IS NULL`` excludes soft-deleted skills.
        """
        row = (
            await session.execute(
                text(
                    """
                    SELECT COUNT(*) AS n FROM skills
                     WHERE status = ANY(:statuses)
                       AND embedding IS NOT NULL
                       AND deleted_at IS NULL
                    """
                ).bindparams(statuses=list(PUBLISHED_STATUSES))
            )
        ).first()
        return int(row.n) if row else 0
