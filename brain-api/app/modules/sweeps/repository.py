"""Data access for onboarding sweeps (the only place sweep API SQL lives).

Runs inside the caller's ``run_in_tenant`` transaction so RLS scopes every query
to the workspace. Progress *writes* happen in the worker via ``JobsRepository``;
this repository only creates and reads.
"""
from __future__ import annotations

import json

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# ``config`` scopes the sweep: ``{"source_ids": [...]}`` limits it to those
# connections, absent/empty means every connected source (the onboarding case).
_COLUMNS = (
    "id, status, config, progress, skills_created, skills_queued, started_at, completed_at"
)


class SweepsRepository:
    async def create(
        self,
        session: AsyncSession,
        *,
        workspace_id: str,
        triggered_by: str | None,
        config: dict | None = None,
    ) -> dict:
        row = (
            await session.execute(
                text(
                    f"""
                    INSERT INTO sweeps (workspace_id, status, triggered_by, config)
                    VALUES (:workspace_id, 'pending', :triggered_by,
                            CAST(:config AS jsonb))
                    RETURNING {_COLUMNS}
                    """
                ).bindparams(
                    workspace_id=workspace_id,
                    triggered_by=triggered_by,
                    config=json.dumps(config) if config else None,
                )
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
        """The workspace's most recent in-flight sweep, whatever its scope.

        For surfaces re-attaching to a sweep they didn't start (``GET /sweeps/active``).
        Starting a sweep uses :meth:`find_covering` instead — "something is running"
        is not the same question as "is my request already being handled".
        """
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

    async def find_covering(
        self, session: AsyncSession, source_ids: list[str] | None = None
    ) -> dict | None:
        """The in-flight sweep that already **covers** this request, if any.

        Coverage, not mere existence: an unscoped sweep covers everything, while a
        scoped one covers only a request for sources it already includes. Without
        that distinction a Slack-scoped backfill would swallow a request for GitHub
        and GitHub would never be imported.

        Two active sweeps can therefore coexist. Over disjoint sources that is fully
        safe — cursors are per-connection and ``sweep_extract`` is per-sweep-id. The
        one overlapping case (a workspace-wide sweep started while a scoped one runs)
        costs duplicate provider fetches, not correctness: event inserts dedupe on the
        unique constraint and cursors only advance past data actually fetched.
        """
        ids = json.dumps(source_ids) if source_ids else None
        row = (
            await session.execute(
                text(
                    f"""
                    SELECT {_COLUMNS} FROM sweeps
                     WHERE status IN ('pending', 'running')
                       AND (
                             COALESCE(jsonb_array_length(config -> 'source_ids'), 0) = 0
                             OR (CAST(:ids AS jsonb) IS NOT NULL
                                 AND config -> 'source_ids' @> CAST(:ids AS jsonb))
                           )
                     ORDER BY started_at DESC
                     LIMIT 1
                    """
                ).bindparams(ids=ids)
            )
        ).mappings().first()
        return dict(row) if row else None
