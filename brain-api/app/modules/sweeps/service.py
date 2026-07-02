"""Sweeps business logic (BACKEND_BEST_PRACTICES.md §2 layering).

Starting a sweep is idempotent per workspace: if one is already pending/running,
that sweep is returned instead of stacking a second backfill over the same
sources (event inserts would dedupe, but the provider API cost would not).
"""
from __future__ import annotations

from app.config.database import get_session
from app.jobs.queue import enqueue
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
    def __init__(self, repository: SweepsRepository | None = None) -> None:
        self._repo = repository or SweepsRepository()

    async def start(self, auth: AuthContext) -> tuple[SweepOut, bool]:
        """Start the onboarding sweep. Returns ``(sweep, created)``.

        ``created`` is False when an in-flight sweep was returned instead.
        """
        workspace_id, role = _require_workspace(auth)
        async with get_session() as session:
            async with run_in_tenant(session, workspace_id, auth.user_id, role):
                active = await self._repo.find_active(session)
                if active is not None:
                    return _out(active), False
                row = await self._repo.create(
                    session, workspace_id=workspace_id, triggered_by=auth.user_id
                )
                await session.commit()

        # Enqueue after commit so the worker can always see the row. Enqueue is
        # best-effort (queue.py swallows outages); a stuck 'pending' sweep is
        # re-enqueued by simply calling start again.
        await enqueue("onboarding_sweep", workspace_id, str(row["id"]))
        return _out(row), True

    async def get(self, auth: AuthContext, sweep_id: str) -> SweepOut:
        workspace_id, role = _require_workspace(auth)
        async with get_session() as session:
            async with run_in_tenant(session, workspace_id, auth.user_id, role):
                row = await self._repo.get(session, sweep_id)
        if row is None:
            raise NotFoundError("Sweep")
        return _out(row)
