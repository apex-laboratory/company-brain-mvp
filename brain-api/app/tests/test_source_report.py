"""Per-source read report (``GET /sources/{id}/report``).

A source that imports 56 items and produces zero skills looks identical to a
broken one unless the discard reasons are visible. The pipeline records a stage
and a reason per event in ``source_events.pipeline_meta``; this is the surface
that shows them.

Service tests stub the repository; router tests drive the ASGI app with auth
overridden. The SQL itself (FILTER rollup, orphaned-row exclusion) is covered
in ``app/tests/e2e/test_backfill_sql.py`` against a real database.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.modules.sources import service as service_module
from app.modules.sources.schemas import SourceReportOut
from app.modules.sources.service import SourcesService
from app.shared.errors.app_error import NotFoundError
from app.shared.middleware.authenticate import AuthContext, get_auth_context

_TOTALS = {"items_read": 56, "skills_kept": 0, "discarded": 56, "pending_items": 0}


class _AsyncCtx:
    def __init__(self, value: object) -> None:
        self._value = value

    async def __aenter__(self) -> object:
        return self._value

    async def __aexit__(self, *_: object) -> bool:
        return False


def _auth() -> AuthContext:
    return AuthContext(user_id="usr_1", workspace_id="wrk_1", role="admin", scopes=[], kind="jwt")


async def _report(
    *,
    status: str | None = "connected",
    totals: dict | None = None,
    groups: list[dict] | None = None,
) -> SourceReportOut:
    repo = MagicMock(
        get_connection_status=AsyncMock(return_value=status),
        connection_event_totals=AsyncMock(return_value=totals or _TOTALS),
        discard_breakdown=AsyncMock(return_value=groups or []),
    )
    svc = SourcesService(repository=repo, sweeps_service=MagicMock())
    session = MagicMock(commit=AsyncMock())
    with patch.object(
        service_module, "get_tenant_session", return_value=_AsyncCtx(session)
    ), patch.object(service_module, "run_in_tenant", return_value=_AsyncCtx(None)):
        return await svc.get_report(_auth(), "src_1")


# ── service ───────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_report_carries_the_totals() -> None:
    report = await _report()
    assert (report.items_read, report.skills_kept, report.discarded) == (56, 0, 56)


@pytest.mark.asyncio
async def test_stages_are_translated_for_the_ui() -> None:
    # The pipeline's stage names are internal vocabulary; the UI must never render
    # "relevance_gate" at a user.
    groups = [
        {"stage": "relevance_gate", "count": 55},
        {"stage": "skill_extractor", "count": 1},
    ]
    report = await _report(groups=groups)
    assert [(g.label, g.count) for g in report.discarded_by_stage] == [
        ("Not durable knowledge", 55),
        ("Extractor abstained", 1),
    ]
    assert report.discarded_by_stage[0].stage == "relevance_gate"  # raw name kept too


@pytest.mark.asyncio
async def test_unknown_stage_degrades_to_its_raw_name() -> None:
    # A stage added to the pipeline later must render as *something*, not a blank row.
    report = await _report(groups=[{"stage": "boundary_check", "count": 2}])
    assert report.discarded_by_stage[0].label == "boundary_check"


@pytest.mark.asyncio
async def test_null_stage_is_survivable() -> None:
    # pipeline_meta predating the stage key yields a NULL stage; must not 500 the report.
    report = await _report(groups=[{"stage": None, "count": 3}])
    group = report.discarded_by_stage[0]
    assert (group.stage, group.label) == ("unknown", "Unknown")


@pytest.mark.asyncio
async def test_healthy_source_reports_an_empty_breakdown() -> None:
    report = await _report(
        totals={"items_read": 12, "skills_kept": 9, "discarded": 0, "pending_items": 3},
        groups=[],
    )
    assert report.discarded_by_stage == []
    assert report.skills_kept == 9


@pytest.mark.asyncio
async def test_unknown_source_is_not_found() -> None:
    with pytest.raises(NotFoundError):
        await _report(status=None)


# ── router ────────────────────────────────────────────────────────────────────
class _StubService:
    async def get_report(self, auth: AuthContext, source_id: str) -> SourceReportOut:
        from app.modules.sources.schemas import DiscardGroupOut

        return SourceReportOut(
            source_id=source_id,
            **_TOTALS,
            discarded_by_stage=[
                DiscardGroupOut(
                    stage="relevance_gate",
                    label="Not durable knowledge",
                    count=55,
                )
            ],
        )


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
async def test_get_report_returns_camelcase_payload(client: AsyncClient) -> None:
    from app.modules.sources import router as router_module

    with patch.object(router_module, "_service", _StubService()):
        resp = await client.get("/api/v1/sources/src_1/report")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["itemsRead"] == 56 and data["skillsKept"] == 0
    group = data["discardedByStage"][0]
    assert group["label"] == "Not durable knowledge"
    assert "sampleReasons" not in group


@pytest.mark.asyncio
async def test_get_report_non_admin_gets_403(client: AsyncClient) -> None:
    _set_auth("editor")
    resp = await client.get("/api/v1/sources/src_1/report")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_get_report_without_credentials_gets_401(client: AsyncClient) -> None:
    from app.main import app

    app.dependency_overrides.clear()  # fall through to real authentication
    resp = await client.get("/api/v1/sources/src_1/report")
    assert resp.status_code == 401
