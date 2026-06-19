"""Service unit tests for the workspace settings module (KAN-64).

Drives ``WorkspaceService`` against a fake repository with the DB session and
tenant context stubbed (no Postgres), asserting authorization (403 for
non-members), the brainEndpoint construction, partial-update field selection, the
empty-PATCH no-op, and 404 mapping.
"""
from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

import pytest

from app.modules.workspaces import service as service_module
from app.modules.workspaces.repository import (
    UpdatedWorkspaceRow,
    WorkspaceRepository,
    WorkspaceSettingsRow,
)
from app.modules.workspaces.service import WorkspaceService, _brain_endpoint
from app.shared.errors.app_error import ForbiddenError, NotFoundError
from app.shared.middleware.authenticate import AuthContext


def _auth(workspace_id: str | None = "wrk_1", role: str = "admin") -> AuthContext:
    return AuthContext(
        user_id="usr_1", workspace_id=workspace_id, role=role, scopes=[], kind="jwt"
    )


class _FakeRepo(WorkspaceRepository):
    def __init__(self, *, row: WorkspaceSettingsRow | None) -> None:
        self._row = row
        self.updated_with: dict[str, str | None] | None = None

    async def get_settings(
        self, session: Any, workspace_id: str
    ) -> WorkspaceSettingsRow | None:
        return self._row

    async def get_identity(
        self, session: Any, workspace_id: str
    ) -> UpdatedWorkspaceRow | None:
        if self._row is None:
            return None
        return UpdatedWorkspaceRow(
            id=self._row.id, name=self._row.name, domain=self._row.domain
        )

    async def update_settings(
        self, session: Any, workspace_id: str, fields: dict[str, str | None]
    ) -> UpdatedWorkspaceRow | None:
        self.updated_with = fields
        if self._row is None:
            return None
        name = fields.get("name", self._row.name) or self._row.name
        domain = fields.get("domain", self._row.domain)
        return UpdatedWorkspaceRow(id=self._row.id, name=name, domain=domain)


def _row() -> WorkspaceSettingsRow:
    return WorkspaceSettingsRow(
        id="wrk_1",
        name="Riverline",
        slug="riverline",
        domain="riverline.io",
        plan="pro",
        seat_limit=12,
    )


class _FakeSession:
    async def commit(self) -> None:  # pragma: no cover - trivial
        pass


@pytest.fixture(autouse=True)
def _stub_session_and_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the DB session + tenant context manager with no-op stand-ins."""

    @contextlib.asynccontextmanager
    async def fake_tenant_session(*_args: Any, **_kwargs: Any) -> AsyncIterator[_FakeSession]:
        yield _FakeSession()

    monkeypatch.setattr(service_module, "tenant_session", fake_tenant_session)


@pytest.mark.asyncio
async def test_get_settings_builds_brain_endpoint() -> None:
    service = WorkspaceService(repository=_FakeRepo(row=_row()))

    result = await service.get_settings(_auth(), "wrk_1")

    assert result.workspace.name == "Riverline"
    assert result.workspace.seat_limit == 12
    assert result.brain_endpoint == _brain_endpoint("riverline")
    assert result.brain_endpoint.startswith("https://riverline.")
    assert result.brain_endpoint.endswith("/mcp")


@pytest.mark.asyncio
async def test_non_member_is_forbidden() -> None:
    service = WorkspaceService(repository=_FakeRepo(row=_row()))

    with pytest.raises(ForbiddenError):
        await service.get_settings(_auth(workspace_id="wrk_OTHER"), "wrk_1")


@pytest.mark.asyncio
async def test_get_settings_missing_workspace_maps_to_404() -> None:
    service = WorkspaceService(repository=_FakeRepo(row=None))

    with pytest.raises(NotFoundError):
        await service.get_settings(_auth(), "wrk_1")


@pytest.mark.asyncio
async def test_update_passes_only_provided_fields() -> None:
    repo = _FakeRepo(row=_row())
    service = WorkspaceService(repository=repo)

    result = await service.update_settings(_auth(), "wrk_1", {"name": "Riverline 2"})

    assert repo.updated_with == {"name": "Riverline 2"}
    assert result.workspace.name == "Riverline 2"
    assert result.workspace.domain == "riverline.io"  # untouched


@pytest.mark.asyncio
async def test_empty_patch_is_noop_returning_current() -> None:
    repo = _FakeRepo(row=_row())
    service = WorkspaceService(repository=repo)

    result = await service.update_settings(_auth(), "wrk_1", {})

    assert repo.updated_with is None  # update_settings never called
    assert result.workspace.id == "wrk_1"
    assert result.workspace.name == "Riverline"


@pytest.mark.asyncio
async def test_update_missing_workspace_maps_to_404() -> None:
    service = WorkspaceService(repository=_FakeRepo(row=None))

    with pytest.raises(NotFoundError):
        await service.update_settings(_auth(), "wrk_1", {"name": "X"})
