"""Dashboard business logic (BACKEND_BEST_PRACTICES.md §2 layering, §8 tenancy).

Framework-agnostic: the router passes the resolved ``AuthContext`` and the path
``workspace_id``; the service enforces membership, opens a tenant-scoped
transaction, fans the reads out across the repository, and shapes the response.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.config.database import get_session
from app.modules.dashboard.repository import (
    KPI_SPECS,
    ActivityRow,
    DashboardRepository,
    DecisionRow,
    MetricAgg,
    ReviewRow,
    SourceRow,
    SyncRow,
)
from app.modules.dashboard.schemas import (
    ActivityEvent,
    ActivityQuery,
    DecisionSummary,
    Kpi,
    OverviewResponse,
    OwnerSummary,
    ReviewSummary,
    SourceSummary,
    SyncState,
    WorkspaceSummary,
)
from app.shared.errors.app_error import ForbiddenError, NotFoundError
from app.shared.middleware.authenticate import AuthContext
from app.shared.middleware.with_tenant import run_in_tenant

# Overview list sizes (kept small — the home screen shows previews, not full lists).
_RECENT_QUESTIONS = 3
_REVIEW_PREVIEW = 5
_RECENT_DECISIONS = 5
_OVERVIEW_ACTIVITY = 8

_SYNC_LABELS = {
    "error": "Sync error — needs attention",
    "syncing": "Syncing sources…",
    "pending": "Sync pending",
    "healthy": "All sources synced",
}


@dataclass(frozen=True)
class ActivityPage:
    events: list[ActivityEvent]
    next_cursor: str | None


class DashboardService:
    def __init__(self, repository: DashboardRepository | None = None) -> None:
        self._repository = repository or DashboardRepository()

    # ── authorization ─────────────────────────────────────────────────────────
    @staticmethod
    def _require_member(auth: AuthContext, workspace_id: str) -> str:
        """Return the caller's workspace, or 403 if they aren't a member of the
        one in the path.

        The JWT/API-key context carries the single workspace the caller belongs
        to; a request for any other ``workspace_id`` is not theirs to read. We
        also drive RLS from the auth context (never the path), so a mismatch
        could not be served anyway — failing closed here makes that explicit.
        """
        if auth.workspace_id is None or workspace_id != auth.workspace_id:
            raise ForbiddenError("You are not a member of this workspace.")
        return auth.workspace_id

    # ── overview ──────────────────────────────────────────────────────────────
    async def get_overview(
        self, auth: AuthContext, workspace_id: str
    ) -> OverviewResponse:
        workspace_id = self._require_member(auth, workspace_id)
        repo = self._repository

        async with get_session() as session, run_in_tenant(
            session, workspace_id, auth.user_id, auth.role or "viewer"
        ):
            workspace = await repo.get_workspace(session, workspace_id)
            if workspace is None:
                raise NotFoundError("Workspace")

            user_name = await repo.get_user_name(session, auth.user_id)
            kpis = [
                self._build_kpi(
                    spec.id,
                    spec.label,
                    await repo.metric(session, workspace_id, spec),
                )
                for spec in KPI_SPECS
            ]
            sync = await repo.get_sync(session, workspace_id)
            questions = await repo.recent_questions(
                session, workspace_id, _RECENT_QUESTIONS
            )
            reviews = await repo.review_preview(
                session, workspace_id, _REVIEW_PREVIEW
            )
            decisions = await repo.recent_decisions(
                session, workspace_id, _RECENT_DECISIONS
            )
            sources = await repo.source_health(session, workspace_id)
            activity = await repo.activity(
                session, workspace_id, limit=_OVERVIEW_ACTIVITY
            )

        return OverviewResponse(
            workspace=WorkspaceSummary(
                name=workspace.name, slug=workspace.slug, plan=workspace.plan
            ),
            greeting_name=_first_name(user_name),
            sync=self._build_sync(sync),
            kpis=kpis,
            recent_questions=questions,
            review_preview=[_to_review(r) for r in reviews],
            recent_decisions=[_to_decision(d) for d in decisions],
            source_health=[_to_source(s) for s in sources],
            activity=[_to_activity(a) for a in activity],
        )

    # ── activity feed ─────────────────────────────────────────────────────────
    async def get_activity(
        self, auth: AuthContext, workspace_id: str, query: ActivityQuery
    ) -> ActivityPage:
        workspace_id = self._require_member(auth, workspace_id)

        async with get_session() as session, run_in_tenant(
            session, workspace_id, auth.user_id, auth.role or "viewer"
        ):
            # Fetch one extra row to know whether another page exists.
            rows = await self._repository.activity(
                session,
                workspace_id,
                limit=query.limit + 1,
                cursor=query.cursor,
            )

        has_more = len(rows) > query.limit
        page = rows[: query.limit]
        next_cursor = page[-1].id if has_more and page else None
        return ActivityPage(
            events=[_to_activity(a) for a in page], next_cursor=next_cursor
        )

    # ── builders ──────────────────────────────────────────────────────────────
    @staticmethod
    def _build_kpi(kpi_id: str, label: str, agg: MetricAgg) -> Kpi:
        return Kpi(
            id=kpi_id,  # ids come from KPI_SPECS (the Literal set)
            label=label,
            value=agg.total,
            trend=_trend(agg.recent, agg.prior),
            spark=agg.spark,
        )

    @staticmethod
    def _build_sync(sync: SyncRow) -> SyncState:
        if sync.connected_count == 0:
            return SyncState(
                status="pending", label="No sources connected", last_synced_at=None
            )
        if sync.any_error:
            status = "error"
        elif sync.any_syncing:
            status = "syncing"
        elif sync.any_pending:
            status = "pending"
        else:
            status = "healthy"
        return SyncState(
            status=status,  # one of the literal statuses set above
            label=_SYNC_LABELS[status],
            last_synced_at=sync.last_synced_at,
        )


def _trend(recent: int, prior: int) -> int:
    """Percentage delta of the recent period vs the prior one (negative = down).

    With no prior-period baseline, any current activity reads as +100% growth and
    none as flat (0%).
    """
    if prior == 0:
        return 100 if recent > 0 else 0
    return round((recent - prior) / prior * 100)


def _first_name(name: str | None) -> str:
    if not name:
        return "there"
    return name.strip().split(" ", 1)[0]


def _to_review(r: ReviewRow) -> ReviewSummary:
    return ReviewSummary(
        id=r.id,
        title=r.title,
        kind=r.kind,
        source_provider=r.source_provider,
        source_location=r.source_location,
        before=r.before_text,
        after=r.after_text,
        evidence_quote=r.evidence_quote,
        evidence_author=r.evidence_author,
        confidence=r.confidence,
        status=r.status,
    )


def _to_decision(d: DecisionRow) -> DecisionSummary:
    owner = (
        OwnerSummary(name=d.owner_name, avatar_color=d.owner_avatar_color)
        if (d.owner_name or d.owner_avatar_color)
        else None
    )
    return DecisionSummary(
        id=d.id,
        title=d.title,
        source_provider=d.source_provider,
        source_location=d.source_location,
        status=d.status,
        confidence=d.confidence,
        category=d.category,
        owner=owner,
        monthly_uses=d.monthly_uses,
        updated_at=d.updated_at,
        summary=d.summary,
        rule=d.rule,
    )


def _to_source(s: SourceRow) -> SourceSummary:
    return SourceSummary(
        id=s.id,
        provider=s.provider,
        name=s.name,
        status=s.status,
        sync_status=s.sync_status,
        last_synced_at=s.last_synced_at,
        health=s.health,
        pending_items=s.pending_items,
        active_channel_count=s.active_channel_count,
        extracted_label=f"{s.decisions_count} decisions",
    )


def _to_activity(a: ActivityRow) -> ActivityEvent:
    return ActivityEvent(
        id=a.id,
        type=a.type,
        title=a.title,
        detail=a.detail,
        source_provider=a.source_provider,
        created_at=a.created_at,
    )
