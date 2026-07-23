"""Data access for the decisions registry (the only place its SQL lives).

Every method runs inside the caller's ``run_in_tenant`` transaction, so RLS scopes
each query to the workspace. Reads join ``users`` for the owner's display name and
filter ``deleted_at IS NULL`` (decisions are soft-deleted). Bound params only.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_DECISION_COLUMNS = """
    d.id, d.title, d.source_provider, d.source_location, d.status, d.confidence,
    d.category, d.monthly_uses, d.updated_at, d.summary, d.rule,
    u.name AS owner_name, u.avatar_color AS owner_avatar_color
"""


class DecisionsRepository:
    async def list_decisions(
        self,
        session: AsyncSession,
        *,
        status: str | None,
        category: str | None,
        source: str | None,
        limit: int,
        cursor: tuple[str, str] | None,
    ) -> list[dict]:
        """One page of decisions, newest-updated first.

        Keyset pagination on ``(updated_at, id)``; the service asks for ``limit + 1``
        to detect a further page."""
        clauses = ["d.deleted_at IS NULL"]
        params: dict = {"limit": limit}
        if status is not None:
            clauses.append("d.status = CAST(:status AS decision_status)")
            params["status"] = status
        if category is not None:
            clauses.append("d.category = :category")
            params["category"] = category
        if source is not None:
            clauses.append("d.source_provider = CAST(:source AS source_provider)")
            params["source"] = source
        if cursor is not None:
            clauses.append(
                "(d.updated_at, d.id) < (CAST(:cursor_ts AS timestamptz), :cursor_id)"
            )
            params["cursor_ts"], params["cursor_id"] = cursor
        where = " AND ".join(clauses)
        rows = (
            await session.execute(
                text(
                    f"SELECT {_DECISION_COLUMNS} "
                    f"FROM decisions d LEFT JOIN users u ON u.id = d.owner_user_id "
                    f"WHERE {where} "
                    f"ORDER BY d.updated_at DESC, d.id DESC "
                    f"LIMIT :limit"
                ).bindparams(**params)
            )
        ).mappings().all()
        return [dict(r) for r in rows]

    async def get_decision(
        self, session: AsyncSession, decision_id: str
    ) -> dict | None:
        """One decision with its owner name, or ``None`` if missing/deleted."""
        row = (
            await session.execute(
                text(
                    f"SELECT {_DECISION_COLUMNS} "
                    f"FROM decisions d LEFT JOIN users u ON u.id = d.owner_user_id "
                    f"WHERE d.id = :id AND d.deleted_at IS NULL"
                ).bindparams(id=decision_id)
            )
        ).mappings().first()
        return dict(row) if row else None
