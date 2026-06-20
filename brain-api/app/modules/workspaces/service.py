"""Workspace settings business logic (BEST_PRACTICES §2 layering, §8 tenancy).

Framework-agnostic: the router passes the resolved ``AuthContext`` and the path
``workspace_id``; the service enforces membership, opens a tenant-scoped
transaction, and shapes the response. The admin-only guard for updates is
applied as a router dependency (``require_role("admin")``) and backstopped by the
``workspaces`` RLS policy.
"""
from __future__ import annotations

from app.config.settings import settings
from app.modules.workspaces.repository import WorkspaceRepository
from app.modules.workspaces.schemas import (
    SettingsResponse,
    UpdatedWorkspace,
    UpdateSettingsResponse,
    WorkspaceConfig,
)
from app.shared.errors.app_error import NotFoundError
from app.shared.middleware.authenticate import AuthContext
from app.shared.middleware.authorize import assert_workspace_member
from app.shared.middleware.with_tenant import tenant_session


class WorkspaceService:
    def __init__(self, repository: WorkspaceRepository | None = None) -> None:
        self._repository = repository or WorkspaceRepository()

    async def get_settings(
        self, auth: AuthContext, workspace_id: str
    ) -> SettingsResponse:
        workspace_id = assert_workspace_member(auth, workspace_id)

        async with tenant_session(auth, workspace_id) as session:
            row = await self._repository.get_settings(session, workspace_id)

        if row is None:
            raise NotFoundError("Workspace")

        return SettingsResponse(
            workspace=WorkspaceConfig(
                name=row.name,
                domain=row.domain,
                plan=row.plan,
                seat_limit=row.seat_limit,
            ),
            brain_endpoint=_brain_endpoint(row.slug),
        )

    async def update_settings(
        self, auth: AuthContext, workspace_id: str, fields: dict[str, str | None]
    ) -> UpdateSettingsResponse:
        workspace_id = assert_workspace_member(auth, workspace_id)

        async with tenant_session(auth, workspace_id) as session:
            if fields:
                row = await self._repository.update_settings(
                    session, workspace_id, fields
                )
                await session.commit()
            else:
                # Empty PATCH: nothing to write — read back the current identity
                # so the response shape is identical to a real update (no-op 200).
                row = await self._repository.get_identity(session, workspace_id)

        if row is None:
            raise NotFoundError("Workspace")

        return UpdateSettingsResponse(
            workspace=UpdatedWorkspace(id=row.id, name=row.name, domain=row.domain)
        )


def _brain_endpoint(slug: str) -> str:
    """Per-workspace MCP endpoint advertised to clients."""
    return f"https://{slug}.{settings.mcp_base_domain}/mcp"
