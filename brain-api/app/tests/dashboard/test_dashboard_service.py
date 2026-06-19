"""Service unit tests for the dashboard module (KAN-58).

Drives ``DashboardService`` against a fake repository with the DB session and
tenant context stubbed (no Postgres), asserting authorization (403 for
non-members), KPI/trend/sync derivation, the response shape, and cursor
pagination.
"""
from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest

from app.modules.dashboard import service as service_module
from app.modules.dashboard.repository import (
    ActivityRow,
    DashboardRepository,
    DecisionRow,
    MetricAgg,
    ReviewRow,
    SourceRow,
    SyncRow,
    WorkspaceRow,
)
from app.modules.dashboard.schemas import ActivityQuery
from app.modules.dashboard.service import (
    DashboardService,
    _first_name,
    _trend,
)
from app.shared.errors.app_error import ForbiddenError
from app.shared.middleware.authenticate import AuthContext

_NOW = datetime(2026, 6, 4, 10, 18, tzinfo=UTC)


def _auth(workspace_id: str | None = "wrk_1") -> AuthContext:
    return AuthContext(
        user_id="usr_1", workspace_id=workspace_id, role="editor", scopes=[], kind="jwt"
    )


class _FakeRepo(DashboardRepository):
    """Canned responses; ``activity`` echoes ``limit`` rows to drive pagination."""

    def __init__(self, *, activity_total: int = 0) -> None:
        self._activity_total = activity_total

    async def get_workspace(self, session: Any, workspace_id: str) -> WorkspaceRow | None:
        return WorkspaceRow(name="Riverline", slug="riverline", plan="pro")

    async def get_user_name(self, session: Any, user_id: str) -> str | None:
        return "Dana Reyes"

    async def metric(self, session: Any, workspace_id: str, spec: Any) -> MetricAgg:
        return MetricAgg(total=184, recent=12, prior=10, spark=[1, 2, 3, 4, 5, 6, 7])

    async def get_sync(self, session: Any, workspace_id: str) -> SyncRow:
        return SyncRow(
            last_synced_at=_NOW,
            any_error=False,
            any_syncing=False,
            any_pending=False,
            connected_count=3,
        )

    async def recent_questions(
        self, session: Any, workspace_id: str, limit: int = 3
    ) -> list[str]:
        return ["How do enterprise discounts get approved?"]

    async def review_preview(
        self, session: Any, workspace_id: str, limit: int = 5
    ) -> list[ReviewRow]:
        return [
            ReviewRow(
                id="rev_1",
                title="Refund window extended",
                kind="policy_change",
                source_provider="slack",
                source_location="#cs-escalations",
                before_text="30 days",
                after_text="45 days",
                evidence_quote="give premium folks 45 days",
                evidence_author="Dana R.",
                confidence=92,
                status="pending",
            )
        ]

    async def recent_decisions(
        self, session: Any, workspace_id: str, limit: int = 5
    ) -> list[DecisionRow]:
        return [
            DecisionRow(
                id="dec_1",
                title="Premium refund exception",
                source_provider="notion",
                source_location="Policy Library",
                status="approved",
                confidence=96,
                category="Support",
                owner_name="Dana Reyes",
                owner_avatar_color="#C2603A",
                monthly_uses=2100,
                updated_at=_NOW,
                summary="Premium-tier customers get a 45-day window.",
                rule="IF tier = premium THEN approve",
            )
        ]

    async def source_health(self, session: Any, workspace_id: str) -> list[SourceRow]:
        return [
            SourceRow(
                id="src_1",
                provider="slack",
                name="Slack",
                status="connected",
                sync_status="healthy",
                last_synced_at=_NOW,
                health=98,
                pending_items=3,
                active_channel_count=18,
                decisions_count=184,
            )
        ]

    async def activity(
        self, session: Any, workspace_id: str, *, limit: int, cursor: str | None = None
    ) -> list[ActivityRow]:
        count = min(limit, self._activity_total)
        return [
            ActivityRow(
                id=f"act_{i:03d}",
                type="skill",
                title="New skill proposed",
                detail="chargeback-triage v3",
                source_provider="github",
                created_at=_NOW,
            )
            for i in range(count)
        ]


@pytest.fixture(autouse=True)
def _stub_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the tenant-scoped session with a no-op (no Postgres)."""

    @contextlib.asynccontextmanager
    async def _fake_tenant_session(*_args: Any, **_kwargs: Any) -> AsyncIterator[object]:
        yield object()

    monkeypatch.setattr(service_module, "tenant_session", _fake_tenant_session)


# ── authorization ────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_overview_rejects_non_member_workspace() -> None:
    service = DashboardService(repository=_FakeRepo())
    with pytest.raises(ForbiddenError):
        await service.get_overview(_auth("wrk_1"), "wrk_OTHER")


@pytest.mark.asyncio
async def test_overview_rejects_user_without_workspace() -> None:
    service = DashboardService(repository=_FakeRepo())
    with pytest.raises(ForbiddenError):
        await service.get_overview(_auth(workspace_id=None), "wrk_1")


@pytest.mark.asyncio
async def test_activity_rejects_non_member_workspace() -> None:
    service = DashboardService(repository=_FakeRepo())
    with pytest.raises(ForbiddenError):
        await service.get_activity(_auth("wrk_1"), "wrk_OTHER", ActivityQuery())


# ── overview shape ───────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_overview_returns_all_eight_fields() -> None:
    service = DashboardService(repository=_FakeRepo())
    overview = await service.get_overview(_auth("wrk_1"), "wrk_1")

    assert overview.workspace.slug == "riverline"
    assert overview.greeting_name == "Dana"  # first name only
    assert overview.sync.status == "healthy"
    assert [k.id for k in overview.kpis] == ["decisions", "policies", "skills", "reviews"]
    assert all(len(k.spark) == 7 for k in overview.kpis)
    assert overview.kpis[0].value == 184
    assert overview.kpis[0].trend == 20  # (12 - 10) / 10 * 100
    assert overview.recent_questions
    assert overview.review_preview[0].before == "30 days"  # before_text -> before
    assert overview.recent_decisions[0].owner is not None
    assert overview.source_health[0].extracted_label == "184 decisions"


@pytest.mark.asyncio
async def test_overview_serializes_to_camel_case() -> None:
    service = DashboardService(repository=_FakeRepo())
    overview = await service.get_overview(_auth("wrk_1"), "wrk_1")
    body = overview.model_dump(by_alias=True)

    assert set(body) == {
        "workspace",
        "greetingName",
        "sync",
        "kpis",
        "recentQuestions",
        "reviewPreview",
        "recentDecisions",
        "sourceHealth",
        "activity",
    }
    assert "lastSyncedAt" in body["sync"]
    assert "sourceProvider" in body["recentDecisions"][0]
    assert "monthlyUses" in body["recentDecisions"][0]


# ── cursor pagination ────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_activity_sets_next_cursor_when_more_pages() -> None:
    # 21 available, limit 20 -> repo gets asked for 21, a next page exists.
    service = DashboardService(repository=_FakeRepo(activity_total=21))
    page = await service.get_activity(_auth("wrk_1"), "wrk_1", ActivityQuery(limit=20))

    assert len(page.events) == 20
    assert page.next_cursor == page.events[-1].id


@pytest.mark.asyncio
async def test_activity_no_next_cursor_on_last_page() -> None:
    service = DashboardService(repository=_FakeRepo(activity_total=5))
    page = await service.get_activity(_auth("wrk_1"), "wrk_1", ActivityQuery(limit=20))

    assert len(page.events) == 5
    assert page.next_cursor is None


# ── derivation helpers ───────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("recent", "prior", "expected"),
    [(12, 10, 20), (3, 4, -25), (5, 0, 100), (0, 0, 0), (8, 8, 0)],
)
def test_trend(recent: int, prior: int, expected: int) -> None:
    assert _trend(recent, prior) == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [("Dana Reyes", "Dana"), ("Dana", "Dana"), (None, "there"), ("", "there")],
)
def test_first_name(name: str | None, expected: str) -> None:
    assert _first_name(name) == expected


@pytest.mark.asyncio
async def test_sync_status_no_sources() -> None:
    class _NoSourcesRepo(_FakeRepo):
        async def get_sync(self, session: Any, workspace_id: str) -> SyncRow:
            return SyncRow(
                last_synced_at=None,
                any_error=False,
                any_syncing=False,
                any_pending=False,
                connected_count=0,
            )

    service = DashboardService(repository=_NoSourcesRepo())
    overview = await service.get_overview(_auth("wrk_1"), "wrk_1")
    assert overview.sync.status == "pending"
    assert overview.sync.label == "No sources connected"
    assert overview.sync.last_synced_at is None


@pytest.mark.asyncio
async def test_sync_status_error_takes_precedence() -> None:
    class _ErrorRepo(_FakeRepo):
        async def get_sync(self, session: Any, workspace_id: str) -> SyncRow:
            return SyncRow(
                last_synced_at=_NOW,
                any_error=True,
                any_syncing=True,
                any_pending=True,
                connected_count=2,
            )

    service = DashboardService(repository=_ErrorRepo())
    overview = await service.get_overview(_auth("wrk_1"), "wrk_1")
    assert overview.sync.status == "error"
