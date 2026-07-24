"""Tests for PATCH /workspaces/:id/onboarding (KAN-51).

Two layers:
  * Service unit tests — drive WorkspaceService with a fake repository,
    asserting next-step progression and partial-update propagation.
  * Router contract tests — drive the FastAPI ASGI app with the service
    stubbed, asserting status codes, camelCase fields, and 403 conditions.
"""
from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.modules.workspaces import service as service_module
from app.modules.workspaces.repository import WorkspaceRepository
from app.modules.workspaces.schemas import OnboardingOut, OnboardingPatchRequest
from app.modules.workspaces.service import WorkspaceService
from app.shared.middleware.authenticate import AuthContext


# ── fakes ────────────────────────────────────────────────────────────────────────

class _FakeSession:
    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass

    async def execute(self, *args: Any, **kwargs: Any) -> None:
        pass


class _FakeWorkspaceRepository(WorkspaceRepository):
    def __init__(self) -> None:
        self.last_update: dict[str, Any] | None = None

    async def create_workspace(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def create_member(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError

    async def update_onboarding(
        self,
        session: Any,
        *,
        workspace_id: str,
        step: str,
        name: str | None,
        team_size: str | None,
        primary_use_case: str | None,
    ) -> None:
        self.last_update = {
            "workspace_id": workspace_id,
            "step": step,
            "name": name,
            "team_size": team_size,
            "primary_use_case": primary_use_case,
        }


@pytest.fixture(autouse=True)
def _patch_session(monkeypatch: pytest.MonkeyPatch) -> None:
    @contextlib.asynccontextmanager
    async def _fake_get_session() -> AsyncIterator[_FakeSession]:
        yield _FakeSession()

    monkeypatch.setattr(service_module, "get_tenant_session", _fake_get_session)


# ── service: next-step progression ───────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "step, expected_next",
    [
        ("company", "connect"),
        ("connect", "configure"),
        ("configure", "build"),
        ("build", "done"),
        ("done", "done"),
    ],
)
async def test_next_step_progression(step: str, expected_next: str) -> None:
    service = WorkspaceService(repository=_FakeWorkspaceRepository())

    result = await service.save_onboarding_step(
        workspace_id="wrk_1",
        user_id="usr_1",
        role="admin",
        request=OnboardingPatchRequest(step=step),
    )

    assert result.status == "saved"
    assert result.next_step == expected_next


@pytest.mark.asyncio
async def test_only_non_null_fields_forwarded_to_repository() -> None:
    repo = _FakeWorkspaceRepository()
    service = WorkspaceService(repository=repo)

    await service.save_onboarding_step(
        workspace_id="wrk_1",
        user_id="usr_1",
        role="admin",
        request=OnboardingPatchRequest(
            step="configure",
            company_name="Updated Name",
            team_size=None,
            primary_use_case=None,
        ),
    )

    assert repo.last_update is not None
    assert repo.last_update["name"] == "Updated Name"
    assert repo.last_update["team_size"] is None
    assert repo.last_update["primary_use_case"] is None
    assert repo.last_update["step"] == "configure"
    assert repo.last_update["workspace_id"] == "wrk_1"


@pytest.mark.asyncio
async def test_ui_only_fields_do_not_reach_repository() -> None:
    repo = _FakeWorkspaceRepository()
    service = WorkspaceService(repository=repo)

    await service.save_onboarding_step(
        workspace_id="wrk_1",
        user_id="usr_1",
        role="admin",
        request=OnboardingPatchRequest(
            step="configure",
            connected_providers=["slack", "notion"],
            time_range="90d",
            channels={"slack": ["#general"]},
        ),
    )

    assert repo.last_update is not None
    assert "connected_providers" not in repo.last_update
    assert "time_range" not in repo.last_update
    assert "channels" not in repo.last_update


# ── router: contract ─────────────────────────────────────────────────────────────

class _StubWorkspaceService:
    def __init__(
        self,
        *,
        result: OnboardingOut | None = None,
        error: Exception | None = None,
    ) -> None:
        self._result = result
        self._error = error

    async def create_workspace(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def save_onboarding_step(
        self, workspace_id: str, user_id: str, role: str, request: Any
    ) -> OnboardingOut:
        if self._error:
            raise self._error
        assert self._result is not None
        return self._result


_ADMIN_AUTH = AuthContext(user_id="usr_1", workspace_id="wrk_1", role="admin")


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from app.main import app
    from app.shared.middleware.rate_limit import limiter

    limiter.enabled = False
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        limiter.enabled = True
        app.dependency_overrides.clear()


def _override(
    service: _StubWorkspaceService,
    auth: AuthContext = _ADMIN_AUTH,
) -> None:
    from app.main import app
    from app.modules.workspaces.router import get_workspace_service
    from app.shared.middleware.authenticate import get_auth_context

    app.dependency_overrides[get_workspace_service] = lambda: service
    app.dependency_overrides[get_auth_context] = lambda: auth


@pytest.mark.asyncio
async def test_patch_onboarding_returns_200_with_next_step(client: AsyncClient) -> None:
    _override(_StubWorkspaceService(result=OnboardingOut(status="saved", next_step="build")))

    resp = await client.patch(
        "/api/v1/workspaces/wrk_1/onboarding",
        json={"step": "configure"},
        headers={"Authorization": "Bearer fake"},
    )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["status"] == "saved"
    assert data["nextStep"] == "build"
    assert resp.json()["meta"]["requestId"]


@pytest.mark.asyncio
async def test_patch_onboarding_viewer_returns_403(client: AsyncClient) -> None:
    viewer = AuthContext(user_id="usr_1", workspace_id="wrk_1", role="viewer")
    _override(
        _StubWorkspaceService(result=OnboardingOut(status="saved", next_step="build")),
        auth=viewer,
    )

    resp = await client.patch(
        "/api/v1/workspaces/wrk_1/onboarding",
        json={"step": "configure"},
        headers={"Authorization": "Bearer fake"},
    )

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


@pytest.mark.asyncio
async def test_patch_onboarding_wrong_workspace_returns_403(client: AsyncClient) -> None:
    wrong = AuthContext(user_id="usr_1", workspace_id="wrk_OTHER", role="admin")
    _override(
        _StubWorkspaceService(result=OnboardingOut(status="saved", next_step="build")),
        auth=wrong,
    )

    resp = await client.patch(
        "/api/v1/workspaces/wrk_1/onboarding",
        json={"step": "configure"},
        headers={"Authorization": "Bearer fake"},
    )

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_patch_onboarding_unknown_step_returns_422(client: AsyncClient) -> None:
    _override(_StubWorkspaceService(result=OnboardingOut(status="saved", next_step="build")))

    resp = await client.patch(
        "/api/v1/workspaces/wrk_1/onboarding",
        json={"step": "UNKNOWN"},
        headers={"Authorization": "Bearer fake"},
    )

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


@pytest.mark.asyncio
async def test_patch_onboarding_invalid_time_range_returns_422(client: AsyncClient) -> None:
    _override(_StubWorkspaceService(result=OnboardingOut(status="saved", next_step="build")))

    resp = await client.patch(
        "/api/v1/workspaces/wrk_1/onboarding",
        json={"step": "configure", "timeRange": "INVALID"},
        headers={"Authorization": "Bearer fake"},
    )

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


@pytest.mark.asyncio
async def test_patch_onboarding_requires_auth(client: AsyncClient) -> None:
    from app.main import app

    app.dependency_overrides.clear()

    resp = await client.patch(
        "/api/v1/workspaces/wrk_1/onboarding",
        json={"step": "configure"},
    )

    assert resp.status_code == 401
