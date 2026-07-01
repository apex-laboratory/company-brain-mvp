"""Sweeps service + router tests (KAN-2).

Service tests stub the repository/queue boundary: starting a sweep is idempotent
(an in-flight sweep is returned, nothing new is created or enqueued) and a fresh
start creates the row *then* enqueues ``onboarding_sweep``. Router tests drive
the ASGI app with auth overridden, asserting the envelope, camelCase fields, and
the admin-only 403.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.modules.sweeps import service as service_module
from app.modules.sweeps.schemas import SweepOut
from app.modules.sweeps.service import SweepsService
from app.shared.errors.app_error import NotFoundError
from app.shared.middleware.authenticate import AuthContext, get_auth_context

_NOW = datetime(2026, 7, 2, 10, 0, tzinfo=UTC)
_ROW = {
    "id": "0b0e8a1c-0000-0000-0000-000000000001",
    "status": "pending",
    "progress": {},
    "skills_created": 0,
    "skills_queued": 0,
    "started_at": _NOW,
    "completed_at": None,
}


class _AsyncCtx:
    def __init__(self, value: object) -> None:
        self._value = value

    async def __aenter__(self) -> object:
        return self._value

    async def __aexit__(self, *_: object) -> bool:
        return False


def _auth() -> AuthContext:
    return AuthContext(user_id="usr_1", workspace_id="wrk_1", role="admin", scopes=[], kind="jwt")


def _service_with(repo: MagicMock) -> tuple[SweepsService, AsyncMock, tuple]:
    svc = SweepsService(repository=repo)
    enqueue = AsyncMock()
    session = MagicMock(commit=AsyncMock())
    patches = (
        patch.object(service_module, "get_session", return_value=_AsyncCtx(session)),
        patch.object(service_module, "run_in_tenant", return_value=_AsyncCtx(None)),
        patch.object(service_module, "enqueue", enqueue),
    )
    return svc, enqueue, patches


# ── service ───────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_start_creates_and_enqueues() -> None:
    repo = MagicMock(find_active=AsyncMock(return_value=None), create=AsyncMock(return_value=_ROW))
    svc, enqueue, patches = _service_with(repo)
    with patches[0], patches[1], patches[2]:
        sweep, created = await svc.start(_auth())
    assert created is True
    assert sweep.id == _ROW["id"]
    enqueue.assert_awaited_once_with("onboarding_sweep", "wrk_1", _ROW["id"])


@pytest.mark.asyncio
async def test_start_returns_inflight_sweep_without_enqueuing() -> None:
    active = {**_ROW, "status": "running"}
    repo = MagicMock(find_active=AsyncMock(return_value=active), create=AsyncMock())
    svc, enqueue, patches = _service_with(repo)
    with patches[0], patches[1], patches[2]:
        sweep, created = await svc.start(_auth())
    assert created is False
    assert sweep.status == "running"
    repo.create.assert_not_awaited()
    enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_unknown_sweep_raises_not_found() -> None:
    repo = MagicMock(get=AsyncMock(return_value=None))
    svc, _, patches = _service_with(repo)
    with patches[0], patches[1], patches[2], pytest.raises(NotFoundError):
        await svc.get(_auth(), "0b0e8a1c-0000-0000-0000-00000000dead")


# ── router ────────────────────────────────────────────────────────────────────
class _StubSweepsService:
    async def start(self, auth: AuthContext) -> tuple[SweepOut, bool]:
        return SweepOut(**{**_ROW, "progress": {"notion": {"status": "running"}}}), True

    async def get(self, auth: AuthContext, sweep_id: str) -> SweepOut:
        return SweepOut(**{**_ROW, "status": "completed", "skills_queued": 4})


def _set_auth(role: str) -> None:
    from app.main import app

    app.dependency_overrides[get_auth_context] = lambda: AuthContext(
        user_id="usr_1", workspace_id="wrk_1", role=role, scopes=[], kind="jwt"
    )


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from app.main import app
    from app.modules.sweeps import router as router_module
    from app.shared.middleware.rate_limit import limiter

    limiter.enabled = False
    _set_auth("admin")
    with patch.object(router_module, "_service", _StubSweepsService()):
        transport = ASGITransport(app=app)
        try:
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                yield c
        finally:
            limiter.enabled = True
            app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_post_sweeps_returns_202_with_progress(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/sweeps")
    assert resp.status_code == 202
    data = resp.json()["data"]
    assert data["status"] == "pending"
    assert data["progress"] == {"notion": {"status": "running"}}
    assert "skillsCreated" in data  # camelCase contract


@pytest.mark.asyncio
async def test_get_sweep_returns_status(client: AsyncClient) -> None:
    resp = await client.get(f"/api/v1/sweeps/{_ROW['id']}")
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "completed"
    assert resp.json()["data"]["skillsQueued"] == 4


@pytest.mark.asyncio
async def test_non_admin_gets_403(client: AsyncClient) -> None:
    _set_auth("editor")
    resp = await client.post("/api/v1/sweeps")
    assert resp.status_code == 403
