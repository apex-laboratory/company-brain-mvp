"""Brain readiness-gate tests (Phase 0).

Service tests stub ``BrainRepository`` and verify the enabled/ready/reason matrix
and the query gate short-circuit. Router tests drive the ASGI app with auth
overridden, asserting the envelope and scope/role gating.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.modules.brain import service as service_module
from app.modules.brain.schemas import BrainStatusResponse
from app.modules.brain.service import BrainService
from app.shared.errors.app_error import BrainNotReadyError
from app.shared.middleware.authenticate import AuthContext, get_auth_context


class _AsyncCtx:
    def __init__(self, value: object) -> None:
        self._value = value

    async def __aenter__(self) -> object:
        return self._value

    async def __aexit__(self, *_: object) -> bool:
        return False


def _auth(role: str = "viewer", kind: str = "jwt", scopes: list[str] | None = None) -> AuthContext:
    return AuthContext(
        user_id="usr_1", workspace_id="wrk_1", role=role,
        scopes=scopes or [], kind=kind,  # type: ignore[arg-type]
    )


def _svc(*, count: int = 3):
    repo = MagicMock(count_indexed_skills=AsyncMock(return_value=count))
    svc = BrainService(repository=repo)
    session = MagicMock(commit=AsyncMock())
    patches = (
        patch.object(service_module, "get_tenant_session", return_value=_AsyncCtx(session)),
        patch.object(service_module, "run_in_tenant", return_value=_AsyncCtx(None)),
    )
    return svc, repo, patches


def _enter(patches):
    for p in patches:
        p.start()


def _exit(patches):
    for p in patches:
        p.stop()


# ── status ─────────────────────────────────────────────────────────────────────

async def test_status_ready_when_skills_indexed() -> None:
    svc, _repo, patches = _svc(count=7)
    _enter(patches)
    try:
        out = await svc.status(_auth())
    finally:
        _exit(patches)
    assert isinstance(out, BrainStatusResponse)
    assert out.enabled and out.ready and out.skills_indexed == 7 and out.reason is None


async def test_status_no_skills_is_not_ready() -> None:
    svc, _repo, patches = _svc(count=0)
    _enter(patches)
    try:
        out = await svc.status(_auth())
    finally:
        _exit(patches)
    assert out.enabled and not out.ready and out.reason == "no_skills"


async def test_status_disabled_by_kill_switch() -> None:
    svc, repo, patches = _svc(count=5)
    _enter(patches)
    try:
        with patch.object(service_module, "settings", MagicMock(brain_chat_enabled=False)):
            out = await svc.status(_auth())
    finally:
        _exit(patches)
    assert not out.enabled and not out.ready and out.reason == "disabled"
    repo.count_indexed_skills.assert_not_awaited()  # short-circuit before any DB hit


# ── query gate ───────────────────────────────────────────────────────────────

async def test_ensure_ready_raises_when_no_skills() -> None:
    svc, _repo, patches = _svc(count=0)
    _enter(patches)
    try:
        with pytest.raises(BrainNotReadyError) as exc:
            await svc._ensure_ready(_auth())
    finally:
        _exit(patches)
    assert exc.value.status == 409 and exc.value.details == {"reason": "no_skills"}


async def test_ensure_ready_raises_when_disabled_without_db_hit() -> None:
    svc, repo, patches = _svc(count=9)
    _enter(patches)
    try:
        with patch.object(service_module, "settings", MagicMock(brain_chat_enabled=False)), \
             pytest.raises(BrainNotReadyError) as exc:
            await svc._ensure_ready(_auth())
    finally:
        _exit(patches)
    assert exc.value.details == {"reason": "disabled"}
    repo.count_indexed_skills.assert_not_awaited()


async def test_ensure_ready_passes_when_indexed() -> None:
    svc, _repo, patches = _svc(count=1)
    _enter(patches)
    try:
        await svc._ensure_ready(_auth())  # no raise
    finally:
        _exit(patches)


# ── router ───────────────────────────────────────────────────────────────────

class _StubService:
    async def status(self, auth):
        return BrainStatusResponse(enabled=True, ready=True, skills_indexed=4, reason=None)


def _set_auth(**kw) -> None:
    from app.main import app
    app.dependency_overrides[get_auth_context] = lambda: _auth(**kw)


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from app.main import app
    from app.modules.brain import router as router_module
    from app.shared.middleware.rate_limit import limiter

    limiter.enabled = False
    _set_auth(role="viewer")
    with patch.object(router_module, "_service", _StubService()):
        transport = ASGITransport(app=app)
        try:
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                yield c
        finally:
            limiter.enabled = True
            app.dependency_overrides.clear()


async def test_status_route_envelope(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/brain/status")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["ready"] is True and data["skillsIndexed"] == 4


async def test_status_requires_auth() -> None:
    from app.main import app
    from app.shared.middleware.rate_limit import limiter

    limiter.enabled = False
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/api/v1/brain/status")  # no credentials
        assert resp.status_code == 401
    finally:
        limiter.enabled = True


async def test_status_api_key_without_scope_forbidden(client: AsyncClient) -> None:
    _set_auth(role="viewer", kind="api_key", scopes=[])  # missing brain:query
    resp = await client.get("/api/v1/brain/status")
    assert resp.status_code == 403


async def test_status_api_key_with_scope_ok(client: AsyncClient) -> None:
    _set_auth(role="viewer", kind="api_key", scopes=["brain:query"])
    resp = await client.get("/api/v1/brain/status")
    assert resp.status_code == 200
