"""Router contract tests for the dashboard module (KAN-58).

Drives the FastAPI app through an ASGI transport with the service and auth
context stubbed, asserting the response envelope, camelCase fields, the
``nextCursor`` pagination meta, the 403 mapping for non-members, and query
validation.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.modules.dashboard.schemas import (
    ActivityEvent,
    Kpi,
    OverviewResponse,
    SyncState,
    UsageResponse,
    WorkspaceSummary,
)
from app.modules.dashboard.service import ActivityPage
from app.shared.errors.app_error import ForbiddenError
from app.shared.middleware.authenticate import AuthContext, get_auth_context

_NOW = datetime(2026, 6, 4, 10, 18, tzinfo=UTC)


def _overview() -> OverviewResponse:
    return OverviewResponse(
        workspace=WorkspaceSummary(name="Riverline", slug="riverline", plan="pro"),
        greeting_name="Dana",
        sync=SyncState(status="healthy", label="All sources synced", last_synced_at=_NOW),
        kpis=[Kpi(id="decisions", label="Decisions", value=184, trend=8, spark=[1] * 7)],
        recent_questions=["How do enterprise discounts get approved?"],
        review_preview=[],
        recent_decisions=[],
        source_health=[],
        activity=[],
    )


def _event(event_id: str) -> ActivityEvent:
    return ActivityEvent(
        id=event_id,
        type="skill",
        title="New skill proposed",
        detail="chargeback-triage v3",
        source_provider="github",
        created_at=_NOW,
    )


class _StubService:
    def __init__(
        self,
        *,
        overview: OverviewResponse | None = None,
        page: ActivityPage | None = None,
        error: Exception | None = None,
    ) -> None:
        self._overview = overview
        self._page = page
        self._error = error

    async def get_overview(self, auth: AuthContext, workspace_id: str) -> OverviewResponse:
        if self._error is not None:
            raise self._error
        assert self._overview is not None
        return self._overview

    async def get_activity(
        self, auth: AuthContext, workspace_id: str, query: object
    ) -> ActivityPage:
        if self._error is not None:
            raise self._error
        assert self._page is not None
        return self._page

    async def get_usage(self, auth: AuthContext, workspace_id: str) -> UsageResponse:
        if self._error is not None:
            raise self._error
        return UsageResponse(
            queries30d=42, skills_served_30d=31, active_skills=7,
            query_series=[0, 1, 2, 3, 4, 5, 6],
        )


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from app.main import app
    from app.shared.middleware.rate_limit import limiter

    limiter.enabled = False  # avoid the Redis-backed limiter in unit tests
    # A request reaches the route only once authenticated; stub the context.
    app.dependency_overrides[get_auth_context] = lambda: AuthContext(
        user_id="usr_1", workspace_id="wrk_1", role="editor", scopes=[], kind="jwt"
    )
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        limiter.enabled = True
        app.dependency_overrides.clear()


def _override(service: _StubService) -> None:
    from app.main import app
    from app.modules.dashboard.router import get_dashboard_service

    app.dependency_overrides[get_dashboard_service] = lambda: service


@pytest.mark.asyncio
async def test_overview_returns_200_envelope(client: AsyncClient) -> None:
    _override(_StubService(overview=_overview()))

    resp = await client.get("/api/v1/workspaces/wrk_1/overview")

    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["greetingName"] == "Dana"
    assert body["data"]["sync"]["lastSyncedAt"] is not None
    assert body["data"]["kpis"][0]["id"] == "decisions"
    assert len(body["data"]["kpis"][0]["spark"]) == 7
    assert body["meta"]["requestId"]
    assert resp.headers["X-Request-ID"]


@pytest.mark.asyncio
async def test_overview_non_member_maps_to_403(client: AsyncClient) -> None:
    _override(_StubService(error=ForbiddenError("You are not a member of this workspace.")))

    resp = await client.get("/api/v1/workspaces/wrk_OTHER/overview")

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


@pytest.mark.asyncio
async def test_activity_returns_next_cursor_meta(client: AsyncClient) -> None:
    page = ActivityPage(events=[_event("act_123")], next_cursor="act_122")
    _override(_StubService(page=page))

    resp = await client.get("/api/v1/workspaces/wrk_1/activity?limit=20")

    assert resp.status_code == 200
    body = resp.json()
    assert body["data"][0]["id"] == "act_123"
    assert body["data"][0]["sourceProvider"] == "github"
    assert body["meta"]["nextCursor"] == "act_122"


@pytest.mark.asyncio
async def test_activity_rejects_limit_over_max(client: AsyncClient) -> None:
    _override(_StubService(page=ActivityPage(events=[], next_cursor=None)))

    resp = await client.get("/api/v1/workspaces/wrk_1/activity?limit=500")

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


@pytest.mark.asyncio
async def test_activity_rejects_unknown_query_param(client: AsyncClient) -> None:
    _override(_StubService(page=ActivityPage(events=[], next_cursor=None)))

    resp = await client.get("/api/v1/workspaces/wrk_1/activity?foo=bar")

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


# ── usage ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_usage_returns_camelcase_counters(client: AsyncClient) -> None:
    _override(_StubService())

    resp = await client.get("/api/v1/workspaces/wrk_1/usage")

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data == {
        "queries30d": 42,
        "skillsServed30d": 31,
        "activeSkills": 7,
        "querySeries": [0, 1, 2, 3, 4, 5, 6],
    }


@pytest.mark.asyncio
async def test_usage_forbidden_for_non_member(client: AsyncClient) -> None:
    _override(_StubService(error=ForbiddenError("You are not a member of this workspace.")))

    resp = await client.get("/api/v1/workspaces/wrk_other/usage")

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"
