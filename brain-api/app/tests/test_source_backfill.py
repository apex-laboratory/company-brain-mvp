"""Per-source historical import tests (``POST /sources/{id}/backfill``).

Connecting a source ingests nothing — historical backfill only ever happened in
the onboarding sweep, which a user can skip. This endpoint is how a connection
made from the dashboard (or one left un-imported by a failed run) gets its past.

Service tests stub the repository and the sweeps service; router tests drive the
ASGI app with auth overridden and assert the envelope plus the auth failures.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.modules.sources import service as service_module
from app.modules.sources.service import SourcesService
from app.modules.sweeps.schemas import SweepOut
from app.shared.errors.app_error import NotFoundError, ValidationError
from app.shared.middleware.authenticate import AuthContext, get_auth_context

_NOW = datetime(2026, 8, 3, 10, 0, tzinfo=UTC)
_SWEEP = SweepOut(
    id="0b0e8a1c-0000-0000-0000-000000000001",
    status="pending",
    progress={},
    skills_created=0,
    skills_queued=0,
    started_at=_NOW,
    completed_at=None,
)


class _AsyncCtx:
    def __init__(self, value: object) -> None:
        self._value = value

    async def __aenter__(self) -> object:
        return self._value

    async def __aexit__(self, *_: object) -> bool:
        return False


def _auth() -> AuthContext:
    return AuthContext(user_id="usr_1", workspace_id="wrk_1", role="admin", scopes=[], kind="jwt")


async def _backfill(status: str | None, sweeps: MagicMock | None = None):
    """Run start_backfill against a connection in ``status`` (None = no such row)."""
    repo = MagicMock(get_connection_status=AsyncMock(return_value=status))
    sweeps = sweeps or MagicMock(start=AsyncMock(return_value=(_SWEEP, True)))
    svc = SourcesService(repository=repo, sweeps_service=sweeps)
    session = MagicMock(commit=AsyncMock())
    with patch.object(
        service_module, "get_tenant_session", return_value=_AsyncCtx(session)
    ), patch.object(service_module, "run_in_tenant", return_value=_AsyncCtx(None)):
        return await svc.start_backfill(_auth(), "src_1"), sweeps


# ── service ───────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_backfill_starts_a_sweep_scoped_to_the_one_connection() -> None:
    (sweep, created), sweeps = await _backfill("connected")
    assert created is True
    assert sweep.id == _SWEEP.id
    sweeps.start.assert_awaited_once()
    assert sweeps.start.await_args.kwargs["source_ids"] == ["src_1"]


@pytest.mark.asyncio
async def test_backfill_reports_an_already_running_import() -> None:
    # SweepsService returns created=False when a sweep already covers this source, so
    # a double click reports the in-flight import rather than stacking a second one.
    sweeps = MagicMock(start=AsyncMock(return_value=(_SWEEP, False)))
    (_sweep, created), _ = await _backfill("connected", sweeps)
    assert created is False


@pytest.mark.asyncio
async def test_backfill_unknown_source_is_not_found() -> None:
    with pytest.raises(NotFoundError):
        await _backfill(None)


@pytest.mark.asyncio
async def test_backfill_refuses_a_source_needing_reauth() -> None:
    # status='error' means the token is dead; queueing an import would fail on the
    # first fetch and leave the connection looking like it is importing.
    with pytest.raises(ValidationError):
        await _backfill("error")


@pytest.mark.asyncio
async def test_backfill_refuses_a_disconnected_source() -> None:
    with pytest.raises(ValidationError):
        await _backfill("disconnected")


# ── router ────────────────────────────────────────────────────────────────────
class _StubService:
    def __init__(self, created: bool = True) -> None:
        self.created = created

    async def start_backfill(self, auth: AuthContext, source_id: str):
        return _SWEEP, self.created


def _set_auth(role: str) -> None:
    from app.main import app

    app.dependency_overrides[get_auth_context] = lambda: AuthContext(
        user_id="usr_1", workspace_id="wrk_1", role=role, scopes=[], kind="jwt"
    )


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from app.main import app
    from app.shared.middleware.rate_limit import limiter

    limiter.enabled = False
    _set_auth("admin")
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        limiter.enabled = True
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_post_backfill_returns_202_for_a_new_import(client: AsyncClient) -> None:
    from app.modules.sources import router as router_module

    with patch.object(router_module, "_service", _StubService()):
        resp = await client.post("/api/v1/sources/src_1/backfill")
    assert resp.status_code == 202
    data = resp.json()["data"]
    assert data["status"] == "pending"
    assert "skillsCreated" in data  # camelCase contract, same shape as POST /sweeps


@pytest.mark.asyncio
async def test_post_backfill_returns_200_when_already_running(client: AsyncClient) -> None:
    from app.modules.sources import router as router_module

    with patch.object(router_module, "_service", _StubService(created=False)):
        resp = await client.post("/api/v1/sources/src_1/backfill")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_post_backfill_non_admin_gets_403(client: AsyncClient) -> None:
    # Importing history spends provider quota and writes to the review queue; it is
    # admin-only like every other connection route.
    _set_auth("editor")
    resp = await client.post("/api/v1/sources/src_1/backfill")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_post_backfill_without_credentials_gets_401(client: AsyncClient) -> None:
    from app.main import app

    app.dependency_overrides.clear()  # fall through to real authentication
    resp = await client.post("/api/v1/sources/src_1/backfill")
    assert resp.status_code == 401
