"""Brain chat business logic (BACKEND_ASKS §7, BRAIN_CHAT_RAG_PLAN).

Phase 0 delivers the readiness gate: chat stays disabled (typed 409) until the
workspace has at least one embedded, reviewed skill, and a global kill-switch can
hard-disable it everywhere. The gate runs *before* any embedding or LLM spend, so a
query against an unready workspace never costs a network call or fabricates an
answer. Phase 1 adds ``query`` (retrieve → synthesize → persist) on top.
"""
from __future__ import annotations

from app.config.database import get_tenant_session
from app.config.settings import settings
from app.modules.brain.repository import BrainRepository
from app.modules.brain.schemas import BrainStatusResponse
from app.shared.errors.app_error import BrainNotReadyError
from app.shared.middleware.authenticate import AuthContext
from app.shared.middleware.with_tenant import run_in_tenant


def _require_workspace(auth: AuthContext) -> tuple[str, str]:
    if auth.workspace_id is None or auth.role is None:
        raise RuntimeError(
            "BUG: brain service called without workspace context — "
            "ensure require_brain_access is declared on this route"
        )
    return auth.workspace_id, auth.role


class BrainService:
    def __init__(self, repository: BrainRepository | None = None) -> None:
        self._repo = repository or BrainRepository()

    async def status(self, auth: AuthContext) -> BrainStatusResponse:
        """Readiness for the caller's workspace (drives the FE disabled state)."""
        if not settings.brain_chat_enabled:
            return BrainStatusResponse(
                enabled=False, ready=False, skills_indexed=0, reason="disabled"
            )
        workspace_id, role = _require_workspace(auth)
        async with get_tenant_session() as session, run_in_tenant(
            session, workspace_id, auth.user_id, role
        ):
            count = await self._repo.count_indexed_skills(session)
        ready = count >= 1
        return BrainStatusResponse(
            enabled=True,
            ready=ready,
            skills_indexed=count,
            reason=None if ready else "no_skills",
        )

    async def _ensure_ready(self, auth: AuthContext) -> None:
        """Gate a query: raise ``BrainNotReadyError`` if disabled or unindexed.

        Runs before embedding/synthesis so a not-ready query short-circuits with a
        typed 409 rather than spending a network call or returning a guess.
        """
        if not settings.brain_chat_enabled:
            raise BrainNotReadyError("disabled", "Brain chat is currently disabled.")
        workspace_id, role = _require_workspace(auth)
        async with get_tenant_session() as session, run_in_tenant(
            session, workspace_id, auth.user_id, role
        ):
            count = await self._repo.count_indexed_skills(session)
        if count < 1:
            raise BrainNotReadyError(
                "no_skills", "No reviewed skills have been indexed for this workspace yet."
            )
