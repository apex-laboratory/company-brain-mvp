"""Data access for onboarding sweeps (the only place sweep API SQL lives).

Runs inside the caller's ``run_in_tenant`` transaction so RLS scopes every query
to the workspace. Progress *writes* happen in the worker via ``JobsRepository``;
this repository only creates and reads.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_COLUMNS = "id, status, progress, skills_created, skills_queued, started_at, completed_at"


class SweepsRepository:
    async def create(
        self, session: AsyncSession, *, workspace_id: str, triggered_by: str | None
    ) -> dict:
        row = (
            await session.execute(
                text(
                    f"""
                    INSERT INTO sweeps (workspace_id, status, triggered_by)
                    VALUES (:workspace_id, 'pending', :triggered_by)
                    RETURNING {_COLUMNS}
                    """
                ).bindparams(workspace_id=workspace_id, triggered_by=triggered_by)
            )
        ).mappings().one()
        return dict(row)

    async def get(self, session: AsyncSession, sweep_id: str) -> dict | None:
        row = (
            await session.execute(
                text(
                    f"SELECT {_COLUMNS} FROM sweeps WHERE id = CAST(:id AS uuid)"
                ).bindparams(id=sweep_id)
            )
        ).mappings().first()
        return dict(row) if row else None

    async def find_active(self, session: AsyncSession) -> dict | None:
        """The workspace's in-flight sweep, if any (one sweep at a time)."""
        row = (
            await session.execute(
                text(
                    f"""
                    SELECT {_COLUMNS} FROM sweeps
                     WHERE status IN ('pending', 'running')
                     ORDER BY started_at DESC
                     LIMIT 1
                    """
                )
            )
        ).mappings().first()
        return dict(row) if row else None
