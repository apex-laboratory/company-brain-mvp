"""Data access for the review queue (the only place review API SQL lives).

Runs inside the caller's ``run_in_tenant`` transaction so RLS scopes every query
to the workspace. Reviews are *written* by the extraction pipeline
(``PipelineRepository.insert_review``); this repository lists, reads, resolves,
and aggregates them for the approval API.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_COLUMNS = (
    "id, title, kind, status, verdict, source_provider, source_location, "
    "before_text, after_text, evidence_quote, evidence_author, confidence, "
    "payload, skill_id, comment, resolved_by, created_at, resolved_at"
)


class ReviewsRepository:
    async def list(
        self,
        session: AsyncSession,
        *,
        status: str | None,
        kind: str | None,
        limit: int,
    ) -> list[dict]:
        """Reviews for the workspace, newest first, optionally filtered.

        Contradictions surface first within a status so the highest-signal items
        lead the queue (PRD Feature 7 ordering).
        """
        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT {_COLUMNS} FROM reviews
                     WHERE (CAST(:status AS text) IS NULL
                            OR status = CAST(:status AS review_status))
                       AND (CAST(:kind AS text) IS NULL
                            OR kind = CAST(:kind AS review_kind))
                     ORDER BY (kind = 'contradiction') DESC, created_at DESC
                     LIMIT :limit
                    """
                ).bindparams(status=status, kind=kind, limit=limit)
            )
        ).mappings().all()
        return [dict(r) for r in rows]

    async def get(
        self, session: AsyncSession, review_id: str, *, for_update: bool = False
    ) -> dict | None:
        """Load one review. ``for_update`` takes a row lock so a concurrent
        approve/reject serializes behind this one (the second caller then sees the
        already-resolved status and 409s) instead of both applying the change."""
        lock = " FOR UPDATE" if for_update else ""
        row = (
            await session.execute(
                text(f"SELECT {_COLUMNS} FROM reviews WHERE id = :id{lock}").bindparams(
                    id=review_id
                )
            )
        ).mappings().first()
        return dict(row) if row else None

    async def resolve(
        self,
        session: AsyncSession,
        review_id: str,
        *,
        status: str,  # approved | rejected
        verdict: str,  # approve | reject
        comment: str | None,
        resolved_by: str | None,
        resolved_at: datetime,
    ) -> bool:
        """Resolve a still-``pending`` review. Returns ``False`` (no row changed)
        if it was already resolved by a concurrent request — the caller treats that
        as a conflict rather than double-applying the skill mutation."""
        result = await session.execute(
            text(
                """
                UPDATE reviews
                   SET status = CAST(:status AS review_status),
                       verdict = :verdict,
                       comment = :comment,
                       resolved_by = :resolved_by,
                       resolved_at = :resolved_at
                 WHERE id = :id AND status = 'pending'
                """
            ).bindparams(
                id=review_id,
                status=status,
                verdict=verdict,
                comment=comment,
                resolved_by=resolved_by,
                resolved_at=resolved_at,
            )
        )
        return result.rowcount == 1

    async def stats(self, session: AsyncSession) -> dict[str, int]:
        """Counts by status + rejection rate instrumentation (PRD §15)."""
        rows = (
            await session.execute(
                text("SELECT status, COUNT(*) AS n FROM reviews GROUP BY status")
            )
        ).mappings().all()
        by_status = {r["status"]: int(r["n"]) for r in rows}
        return {
            "pending": by_status.get("pending", 0),
            "approved": by_status.get("approved", 0),
            "rejected": by_status.get("rejected", 0),
        }

    async def oldest_pending_at(self, session: AsyncSession) -> datetime | None:
        """When the longest-waiting pending review was queued (``None`` if none).

        Backlog age, not just count, is what a reminder should escalate on — a
        review queued a minute ago and one ignored for a fortnight look identical
        to a bare count.
        """
        return (
            await session.execute(
                text("SELECT MIN(created_at) FROM reviews WHERE status = 'pending'")
            )
        ).scalar_one_or_none()
