"""Dashboard data access — the only place dashboard SQL lives (§2 layering).

Every query is workspace-scoped (``workspace_id = :workspace_id`` bound param)
and runs under the caller's tenant context (RLS backstop). All values are bound
parameters; the only interpolated SQL fragments are the KPI ``_MetricSpec``
table/predicate constants below, which are module-level literals and never carry
request data (§5 injection rule).

The overview is assembled from a fixed, small set of queries (no per-row
queries → no N+1). They run sequentially on a single tenant-scoped session: a
SQLAlchemy ``AsyncSession`` multiplexes one connection, so concurrent statements
on it are unsafe — "parallel queries" in the best-practices doc means separate
connections, which is not worth the extra RLS round-trips here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


# ── row projections ─────────────────────────────────────────────────────────
@dataclass(frozen=True)
class WorkspaceRow:
    name: str
    slug: str
    plan: str


@dataclass(frozen=True)
class MetricAgg:
    """Raw aggregates for one KPI; the service derives value/trend from these."""

    total: int
    recent: int  # created in the last 7 days
    prior: int  # created in the 7 days before that
    spark: list[int]  # 7 cumulative totals at each day-end, oldest first


@dataclass(frozen=True)
class SyncRow:
    last_synced_at: datetime | None
    any_error: bool
    any_syncing: bool
    any_pending: bool
    connected_count: int


@dataclass(frozen=True)
class ReviewRow:
    id: str
    title: str
    kind: str
    source_provider: str | None
    source_location: str | None
    before_text: str | None
    after_text: str | None
    evidence_quote: str | None
    evidence_author: str | None
    confidence: int | None
    status: str


@dataclass(frozen=True)
class DecisionRow:
    id: str
    title: str
    source_provider: str | None
    source_location: str | None
    status: str
    confidence: int | None
    category: str | None
    owner_name: str | None
    owner_avatar_color: str | None
    monthly_uses: int
    updated_at: datetime
    summary: str | None
    rule: str | None


@dataclass(frozen=True)
class SourceRow:
    id: str
    provider: str
    name: str
    status: str
    sync_status: str
    last_synced_at: datetime | None
    health: int | None
    pending_items: int
    active_channel_count: int
    decisions_count: int


@dataclass(frozen=True)
class ActivityRow:
    id: str
    type: str
    title: str
    detail: str | None
    source_provider: str | None
    created_at: datetime


# ── KPI specs ───────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class _MetricSpec:
    id: str
    label: str
    table: str  # trusted constant — never request data
    predicate: str  # trusted constant — never request data


# "policies" are company policies: decisions categorised as such. The label and
# predicate are the single point to retarget if a dedicated policy entity lands.
KPI_SPECS: tuple[_MetricSpec, ...] = (
    _MetricSpec("decisions", "Decisions", "decisions", "deleted_at IS NULL"),
    _MetricSpec(
        "policies",
        "Policies",
        "decisions",
        "deleted_at IS NULL AND lower(category) = 'policy'",
    ),
    _MetricSpec(
        "skills",
        "Skills live",
        "skills",
        "deleted_at IS NULL AND status IN ('stable', 'active')",
    ),
    _MetricSpec("reviews", "Awaiting review", "reviews", "status = 'pending'"),
)


class DashboardRepository:
    """Stateless repository; methods take the session they run in."""

    async def get_workspace(
        self, session: AsyncSession, workspace_id: str
    ) -> WorkspaceRow | None:
        row = (
            await session.execute(
                text(
                    "SELECT name, slug, plan FROM workspaces "
                    "WHERE id = :workspace_id AND deleted_at IS NULL"
                ).bindparams(workspace_id=workspace_id),
            )
        ).first()
        if row is None:
            return None
        return WorkspaceRow(name=row.name, slug=row.slug, plan=str(row.plan))

    async def get_user_name(self, session: AsyncSession, user_id: str) -> str | None:
        row = (
            await session.execute(
                text("SELECT name FROM users WHERE id = :user_id").bindparams(
                    user_id=user_id
                ),
            )
        ).first()
        return row.name if row is not None else None

    async def metric(
        self, session: AsyncSession, workspace_id: str, spec: _MetricSpec
    ) -> MetricAgg:
        """Aggregate one KPI from ``created_at`` (no historical snapshot table).

        ``spec.table``/``spec.predicate`` are module constants; ``workspace_id``
        is bound. ``spark`` is the cumulative total at the end of each of the
        last 7 days; ``recent``/``prior`` are the trailing-7-day windows used for
        the trend.
        """
        sql = f"""
            WITH t AS (
                SELECT created_at FROM {spec.table}
                WHERE workspace_id = :workspace_id AND {spec.predicate}
            ),
            days AS (
                SELECT generate_series(
                    date_trunc('day', now()) - interval '6 days',
                    date_trunc('day', now()),
                    interval '1 day'
                ) AS day
            )
            SELECT
                (SELECT count(*) FROM t) AS total,
                (SELECT count(*) FROM t
                    WHERE created_at >= now() - interval '7 days') AS recent,
                (SELECT count(*) FROM t
                    WHERE created_at >= now() - interval '14 days'
                      AND created_at <  now() - interval '7 days') AS prior,
                (SELECT array_agg(c ORDER BY day) FROM (
                    SELECT d.day,
                           (SELECT count(*) FROM t
                              WHERE t.created_at < d.day + interval '1 day') AS c
                    FROM days d
                ) s) AS spark
        """  # table/predicate are trusted module constants, never request input

        row = (
            await session.execute(
                text(sql).bindparams(workspace_id=workspace_id)
            )
        ).one()
        spark = [int(x) for x in (row.spark or [])]
        return MetricAgg(
            total=int(row.total),
            recent=int(row.recent),
            prior=int(row.prior),
            spark=spark,
        )

    async def get_sync(self, session: AsyncSession, workspace_id: str) -> SyncRow:
        row = (
            await session.execute(
                text(
                    """
                    SELECT
                        max(last_synced_at) AS last_synced_at,
                        bool_or(sync_status = 'error')   AS any_error,
                        bool_or(sync_status = 'syncing') AS any_syncing,
                        bool_or(sync_status = 'pending') AS any_pending,
                        count(*) AS connected_count
                    FROM source_connections
                    WHERE workspace_id = :workspace_id AND status = 'connected'
                    """
                ).bindparams(workspace_id=workspace_id),
            )
        ).one()
        return SyncRow(
            last_synced_at=row.last_synced_at,
            any_error=bool(row.any_error),
            any_syncing=bool(row.any_syncing),
            any_pending=bool(row.any_pending),
            connected_count=int(row.connected_count),
        )

    async def recent_questions(
        self, session: AsyncSession, workspace_id: str, limit: int = 3
    ) -> list[str]:
        """The last ``limit`` questions asked by workspace members (newest first)."""
        rows = (
            await session.execute(
                text(
                    """
                    SELECT content FROM brain_messages
                    WHERE workspace_id = :workspace_id AND role = 'user'
                    ORDER BY created_at DESC, id DESC
                    LIMIT :limit
                    """
                ).bindparams(workspace_id=workspace_id, limit=limit),
            )
        ).all()
        return [row.content for row in rows]

    async def review_preview(
        self, session: AsyncSession, workspace_id: str, limit: int = 5
    ) -> list[ReviewRow]:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT id, title, kind, source_provider, source_location,
                           before_text, after_text, evidence_quote, evidence_author,
                           confidence, status
                    FROM reviews
                    WHERE workspace_id = :workspace_id AND status = 'pending'
                    ORDER BY created_at DESC, id DESC
                    LIMIT :limit
                    """
                ).bindparams(workspace_id=workspace_id, limit=limit),
            )
        ).all()
        return [
            ReviewRow(
                id=r.id,
                title=r.title,
                kind=r.kind,
                source_provider=r.source_provider,
                source_location=r.source_location,
                before_text=r.before_text,
                after_text=r.after_text,
                evidence_quote=r.evidence_quote,
                evidence_author=r.evidence_author,
                confidence=r.confidence,
                status=r.status,
            )
            for r in rows
        ]

    async def recent_decisions(
        self, session: AsyncSession, workspace_id: str, limit: int = 5
    ) -> list[DecisionRow]:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT d.id, d.title, d.source_provider, d.source_location,
                           d.status, d.confidence, d.category, d.monthly_uses,
                           d.updated_at, d.summary, d.rule,
                           u.name AS owner_name, u.avatar_color AS owner_avatar_color
                    FROM decisions d
                    LEFT JOIN users u ON u.id = d.owner_user_id
                    WHERE d.workspace_id = :workspace_id AND d.deleted_at IS NULL
                    ORDER BY d.updated_at DESC, d.id DESC
                    LIMIT :limit
                    """
                ).bindparams(workspace_id=workspace_id, limit=limit),
            )
        ).all()
        return [
            DecisionRow(
                id=r.id,
                title=r.title,
                source_provider=r.source_provider,
                source_location=r.source_location,
                status=r.status,
                confidence=r.confidence,
                category=r.category,
                owner_name=r.owner_name,
                owner_avatar_color=r.owner_avatar_color,
                monthly_uses=int(r.monthly_uses),
                updated_at=r.updated_at,
                summary=r.summary,
                rule=r.rule,
            )
            for r in rows
        ]

    async def source_health(
        self, session: AsyncSession, workspace_id: str
    ) -> list[SourceRow]:
        """All connections with derived counts (one query, LATERAL aggregates)."""
        rows = (
            await session.execute(
                text(
                    """
                    SELECT
                        sc.id, sc.provider, sc.name, sc.status, sc.sync_status,
                        sc.last_synced_at, sc.health,
                        COALESCE(ev.pending, 0)        AS pending_items,
                        COALESCE(ch.active_channels, 0) AS active_channel_count,
                        COALESCE(dc.decisions, 0)       AS decisions_count
                    FROM source_connections sc
                    LEFT JOIN LATERAL (
                        SELECT count(*) AS pending FROM source_events e
                        WHERE e.workspace_id = sc.workspace_id
                          AND e.source_id = sc.id
                          AND e.processed = FALSE
                    ) ev ON TRUE
                    LEFT JOIN LATERAL (
                        SELECT count(*) AS active_channels FROM source_channels c
                        WHERE c.source_id = sc.id AND c.selected = TRUE
                    ) ch ON TRUE
                    LEFT JOIN LATERAL (
                        SELECT count(*) AS decisions FROM decisions d
                        WHERE d.workspace_id = sc.workspace_id
                          AND d.source_provider = sc.provider
                          AND d.deleted_at IS NULL
                    ) dc ON TRUE
                    WHERE sc.workspace_id = :workspace_id
                    ORDER BY sc.created_at ASC, sc.id ASC
                    """
                ).bindparams(workspace_id=workspace_id),
            )
        ).all()
        return [
            SourceRow(
                id=r.id,
                provider=r.provider,
                name=r.name,
                status=r.status,
                sync_status=r.sync_status,
                last_synced_at=r.last_synced_at,
                health=r.health,
                pending_items=int(r.pending_items),
                active_channel_count=int(r.active_channel_count),
                decisions_count=int(r.decisions_count),
            )
            for r in rows
        ]

    async def activity(
        self,
        session: AsyncSession,
        workspace_id: str,
        *,
        limit: int,
        cursor: str | None = None,
    ) -> list[ActivityRow]:
        """Activity events newest-first.

        Keyset pagination on the ``act_`` ULID id: ids embed creation time and
        sort lexicographically, so ``id < :cursor ORDER BY id DESC`` returns the
        page after ``cursor`` — consistent with ``created_at`` ordering without a
        cursor-row lookup. The caller fetches ``limit + 1`` rows to detect a next
        page.
        """
        params: dict[str, object] = {"workspace_id": workspace_id, "limit": limit}
        cursor_clause = ""
        if cursor is not None:
            cursor_clause = "AND id < :cursor"
            params["cursor"] = cursor
        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT id, type, title, detail, source_provider, created_at
                    FROM activity_events
                    WHERE workspace_id = :workspace_id {cursor_clause}
                    ORDER BY created_at DESC, id DESC
                    LIMIT :limit
                    """
                ).bindparams(**params),
            )
        ).all()
        return [
            ActivityRow(
                id=r.id,
                type=r.type,
                title=r.title,
                detail=r.detail,
                source_provider=r.source_provider,
                created_at=r.created_at,
            )
            for r in rows
        ]
