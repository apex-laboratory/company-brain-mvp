"""Decisions registry service + router tests (BACKEND_ASKS §8).

Service tests stub the repository and verify the row→``DecisionOut`` mapping,
cursor pagination, and not-found. Router tests drive the ASGI app with auth
overridden, asserting the envelope, filters, and role gating.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.modules.decisions import service as service_module
from app.modules.decisions.service import DecisionsService
from app.shared.errors.app_error import NotFoundError
from app.shared.middleware.authenticate import AuthContext, get_auth_context

_NOW = datetime(2026, 7, 5, 10, 0, tzinfo=UTC)


class _AsyncCtx:
    def __init__(self, value: object) -> None:
        self._value = value

    async def __aenter__(self) -> object:
        return self._value

    async def __aexit__(self, *_: object) -> bool:
        return False


def _auth(role: str = "viewer", kind: str = "jwt", scopes=None) -> AuthContext:
    return AuthContext(
        user_id="usr_1", workspace_id="wrk_1", role=role,
        scopes=scopes or [], kind=kind,  # type: ignore[arg-type]
    )


def _row(**over) -> dict:
    base = {
        "id": "dec_1", "title": "Refund policy", "source_provider": "notion",
        "source_location": "Policy Library", "status": "active", "confidence": 96,
        "category": "policy", "monthly_uses": 210, "updated_at": _NOW,
        "summary": "Refund within 30 days.", "rule": "if days<=30: refund()",
        "owner_name": "Ada", "owner_avatar_color": "#abc",
    }
    return {**base, **over}


def _svc(*, rows=None, one=None):
    repo = MagicMock(
        list_decisions=AsyncMock(return_value=rows or []),
        get_decision=AsyncMock(return_value=one),
    )
    svc = DecisionsService(repository=repo)
    session = MagicMock()
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


async def test_list_maps_fields_and_owner() -> None:
    svc, _repo, patches = _svc(rows=[_row()])
    _enter(patches)
    try:
        items, cursor = await svc.list(
            _auth(), status=None, category=None, source=None, limit=10, cursor=None
        )
    finally:
        _exit(patches)
    d = items[0]
    assert d.provider == "notion" and d.location == "Policy Library"
    assert d.uses == 210 and d.body == "Refund within 30 days."
    assert d.owner is not None and d.owner.name == "Ada"
    assert cursor is None  # only one row, no further page


async def test_list_returns_cursor_when_more() -> None:
    svc, _repo, patches = _svc(rows=[_row(), _row(id="dec_2")])  # limit+1
    _enter(patches)
    try:
        items, cursor = await svc.list(
            _auth(), status=None, category=None, source=None, limit=1, cursor=None
        )
    finally:
        _exit(patches)
    assert len(items) == 1 and cursor is not None


async def test_get_not_found() -> None:
    svc, _repo, patches = _svc(one=None)
    _enter(patches)
    try:
        with pytest.raises(NotFoundError):
            await svc.get(_auth(), "dec_missing")
    finally:
        _exit(patches)


async def test_get_omits_owner_when_absent() -> None:
    svc, _repo, patches = _svc(one=_row(owner_name=None, owner_avatar_color=None))
    _enter(patches)
    try:
        d = await svc.get(_auth(), "dec_1")
    finally:
        _exit(patches)
    assert d.owner is None


# ── router ───────────────────────────────────────────────────────────────────

class _StubService:
    async def list(self, auth, *, status, category, source, limit, cursor):
        from app.modules.decisions.schemas import DecisionOut
        return [DecisionOut(id="dec_1", title="Refund policy", status="active",
                            provider="notion", uses=210, rule="r")], "cur_2"

    async def get(self, auth, decision_id):
        from app.modules.decisions.schemas import DecisionOut
        return DecisionOut(id=decision_id, title="Refund policy", status="active",
                           body="Refund within 30 days.", rule="r")


def _set_auth(**kw) -> None:
    from app.main import app
    app.dependency_overrides[get_auth_context] = lambda: _auth(**kw)


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from app.main import app
    from app.modules.decisions import router as router_module
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


async def test_list_route_envelope(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/decisions?limit=10")
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"][0]["provider"] == "notion"
    assert body["data"][0]["uses"] == 210
    assert body["meta"]["nextCursor"] == "cur_2"


async def test_list_route_rejects_bad_status(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/decisions?status=bogus")
    assert resp.status_code == 422


async def test_get_route(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/decisions/dec_1")
    assert resp.status_code == 200
    assert resp.json()["data"]["body"] == "Refund within 30 days."


async def test_list_requires_auth_role(client: AsyncClient) -> None:
    _set_auth(role=None)  # no role → below viewer
    resp = await client.get("/api/v1/decisions")
    assert resp.status_code == 403
