"""Sweeps business logic (BACKEND_BEST_PRACTICES.md §2 layering).

Starting a sweep is idempotent per workspace: if one is already pending/running,
that sweep is returned instead of stacking a second backfill over the same
sources (event inserts would dedupe, but the provider API cost would not).
"""
from __future__ import annotations

from uuid import UUID

from app.config.database import get_tenant_session
from app.jobs.queue import enqueue
from app.jobs.repository import JobsRepository
from app.modules.sweeps.repository import SweepsRepository
from app.modules.sweeps.schemas import SweepOut
from app.shared.errors.app_error import NotFoundError
from app.shared.middleware.authenticate import AuthContext
from app.shared.middleware.with_tenant import run_in_tenant


def _require_workspace(auth: AuthContext) -> tuple[str, str]:
    """Narrow auth to a workspace-scoped context (see sources service)."""
    if auth.workspace_id is None or auth.role is None:
        raise RuntimeError(
            "BUG: sweeps service called without workspace context — "
            "ensure require_role is declared on this route"
        )
    return auth.workspace_id, auth.role


def _out(row: dict) -> SweepOut:
    return SweepOut(
        id=str(row["id"]),
        status=row["status"],
        progress=row.get("progress") or {},
        skills_created=row.get("skills_created") or 0,
        skills_queued=row.get("skills_queued") or 0,
        started_at=row["started_at"],
        completed_at=row.get("completed_at"),
    )


class SweepsService:
    def __init__(
        self,
        repository: SweepsRepository | None = None,
        jobs_repository: JobsRepository | None = None,
    ) -> None:
        self._repo = repository or SweepsRepository()
        # Connection-level writes (sync_status) live in the jobs repository, which
        # already owns source_connections SQL for the worker.
        self._jobs_repo = jobs_repository or JobsRepository()

    async def start(
        self, auth: AuthContext, source_ids: list[str] | None = None
    ) -> tuple[SweepOut, bool]:
        """Start a sweep. Returns ``(sweep, created)``.

        ``created`` is False when an in-flight sweep was returned instead.

        ``source_ids`` scopes the sweep to specific connections — the per-source
        historical import (``POST /sources/{id}/backfill`` and the dashboard OAuth
        callback). Omitted, this is the onboarding sweep across every connected
        source, unchanged. The scope is stored in ``sweeps.config`` and read back by
        the ``onboarding_sweep`` job.
        """
        workspace_id, role = _require_workspace(auth)
        config = {"source_ids": source_ids} if source_ids else None
        async with get_tenant_session() as session:
            async with run_in_tenant(session, workspace_id, auth.user_id, role):
                active = await self._repo.find_covering(session, source_ids)
                if active is None:
                    active = await self._repo.create(
                        session,
                        workspace_id=workspace_id,
                        triggered_by=auth.user_id,
                        config=config,
                    )
                    created = True
                    # Surface the queued import on the connection itself, so the
                    # Sources page can show it without holding a sweep id.
                    if source_ids:
                        await self._jobs_repo.mark_syncing(session, source_ids)
                else:
                    created = False
                await session.commit()

        # Enqueue after commit so the worker can always see the row. Enqueue is
        # best-effort (queue.py swallows outages), so a sweep still 'pending' has never
        # been picked up — its original enqueue may have been dropped. Re-enqueue it
        # (a 'running' sweep is already in flight, so leave it alone). ARQ's job id
        # dedupes a genuine duplicate.
        if created or active["status"] == "pending":
            await enqueue(
                "onboarding_sweep", workspace_id, str(active["id"]),
                _job_id=f"onboarding-sweep:{active['id']}",
            )
        return _out(active), created

    async def get_active(self, auth: AuthContext) -> SweepOut | None:
        """The workspace's most recent in-flight sweep, or ``None``.

        Lets a surface re-attach to a sweep it didn't start — a dashboard reload
        mid-import, or onboarding resuming after a refresh (which previously had no
        way to look up the running sweep and dropped the user back on the CTA).
        Deliberately unfiltered by scope: the caller wants whatever is running.
        """
        workspace_id, role = _require_workspace(auth)
        async with get_tenant_session() as session:
            async with run_in_tenant(session, workspace_id, auth.user_id, role):
                row = await self._repo.find_active(session)
        return _out(row) if row else None

    async def get(self, auth: AuthContext, sweep_id: str) -> SweepOut:
        workspace_id, role = _require_workspace(auth)
        # Sweep ids are UUIDs; a non-UUID path param is a 404, not a DB cast error (500).
        try:
            UUID(sweep_id)
        except ValueError:
            raise NotFoundError("Sweep") from None
        async with get_tenant_session() as session:
            async with run_in_tenant(session, workspace_id, auth.user_id, role):
                row = await self._repo.get(session, sweep_id)
        if row is None:
            raise NotFoundError("Sweep")
        return _out(row)
