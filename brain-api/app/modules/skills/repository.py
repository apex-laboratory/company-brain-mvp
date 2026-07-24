"""Data access for the skills delivery surface (the only place its SQL lives).

Every method runs inside the caller's ``run_in_tenant`` transaction, so RLS scopes
each query to the workspace; ``workspace_id`` is also bound explicitly on writes
(defense in depth). Reads never expose the ``embedding`` column. Vector search
itself is not here — the service reuses the tested ``PipelineRepository.similar_skills``
HNSW query; this repository serves full bodies, versions, export, the
confidence-decrement feedback write, and interaction logging.
"""
from __future__ import annotations

import json

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.shared.helpers.ids import generate_id

# "Published" for delivery = agent-queryable states.
PUBLISHED_STATUSES: tuple[str, ...] = ("active", "stable")

_SKILL_COLUMNS = (
    "id, name, version, status, trigger, base_logic, exceptions_block, actions, "
    "source_authority, confidence, created_at, updated_at"
)

# Columns for the browse-the-registry list (no embedding, no full actions body).
_LIST_COLUMNS = (
    "id, name, version, status, base_logic, exceptions_block, source_authority, "
    "source_providers, description, calls_30d, updated_at"
)


def _vector_literal(embedding: list[float]) -> str:
    """pgvector input literal: '[0.1,0.2,...]' (mirrors the pipeline repo helper)."""
    return "[" + ",".join(repr(float(v)) for v in embedding) + "]"


class SkillsRepository:
    async def get(
        self,
        session: AsyncSession,
        skill_id: str,
        *,
        statuses: tuple[str, ...] | None = None,
    ) -> dict | None:
        """Full skill body by id (never deleted). ``statuses`` restricts to those
        states (e.g. published-only for the agent surface)."""
        clause = " AND status = ANY(:statuses)" if statuses is not None else ""
        stmt = text(
            f"SELECT {_SKILL_COLUMNS} FROM skills "
            f"WHERE id = :id AND deleted_at IS NULL{clause}"
        ).bindparams(id=skill_id)
        if statuses is not None:
            stmt = stmt.bindparams(statuses=list(statuses))
        row = (await session.execute(stmt)).mappings().first()
        return dict(row) if row else None

    async def list_versions(self, session: AsyncSession, skill_id: str) -> list[dict]:
        """Version history for a skill, oldest first."""
        rows = (
            await session.execute(
                text(
                    """
                    SELECT version, base_logic, exceptions_block, confidence,
                           change_type, created_at
                      FROM skill_versions
                     WHERE skill_id = :id
                     ORDER BY created_at ASC
                    """
                ).bindparams(id=skill_id)
            )
        ).mappings().all()
        return [dict(r) for r in rows]

    async def list_published(
        self, session: AsyncSession, statuses: tuple[str, ...] = PUBLISHED_STATUSES
    ) -> list[dict]:
        """Every published skill's full body, for the export bundle (name order)."""
        rows = (
            await session.execute(
                text(
                    f"SELECT {_SKILL_COLUMNS} FROM skills "
                    f"WHERE status = ANY(:statuses) AND deleted_at IS NULL "
                    f"ORDER BY name ASC"
                ).bindparams(statuses=list(statuses))
            )
        ).mappings().all()
        return [dict(r) for r in rows]

    async def list_skills(
        self,
        session: AsyncSession,
        *,
        statuses: tuple[str, ...] | None,
        source: str | None,
        limit: int,
        cursor: tuple[str, str] | None,
    ) -> list[dict]:
        """One page of the registry, newest-updated first.

        Keyset pagination on ``(updated_at, id)`` — the ``cursor`` is the last row
        of the previous page as ``(updated_at_iso, id)``. Fetches ``limit`` rows;
        the service asks for ``limit + 1`` to detect a further page. ``source``
        matches against the ``source_providers`` array."""
        clauses = ["deleted_at IS NULL"]
        params: dict = {"limit": limit}
        if statuses is not None:
            clauses.append("status = ANY(:statuses)")
            params["statuses"] = list(statuses)
        if source is not None:
            clauses.append(":source = ANY(source_providers)")
            params["source"] = source
        if cursor is not None:
            # Rows strictly after the cursor in (updated_at DESC, id DESC) order.
            clauses.append(
                "(updated_at, id) < (CAST(:cursor_ts AS timestamptz), :cursor_id)"
            )
            params["cursor_ts"], params["cursor_id"] = cursor
        where = " AND ".join(clauses)
        rows = (
            await session.execute(
                text(
                    f"SELECT {_LIST_COLUMNS} FROM skills "
                    f"WHERE {where} "
                    f"ORDER BY updated_at DESC, id DESC "
                    f"LIMIT :limit"
                ).bindparams(**params)
            )
        ).mappings().all()
        return [dict(r) for r in rows]

    async def call_series(
        self, session: AsyncSession, skill_ids: list[str], *, days: int = 7
    ) -> dict[str, dict[str, int]]:
        """Daily interaction counts per skill for the last ``days`` days.

        Returns ``{skill_id: {iso_date: count}}``; the service densifies each into a
        fixed-length zero-filled sparkline. One batched query for the whole page."""
        if not skill_ids:
            return {}
        rows = (
            await session.execute(
                text(
                    """
                    SELECT skill_id,
                           (date_trunc('day', created_at))::date AS day,
                           COUNT(*) AS n
                      FROM agent_interactions
                     WHERE skill_id = ANY(:ids)
                       AND created_at >= (now() - make_interval(days => :days))
                     GROUP BY skill_id, day
                    """
                ).bindparams(ids=skill_ids, days=days)
            )
        ).mappings().all()
        out: dict[str, dict[str, int]] = {}
        for r in rows:
            out.setdefault(str(r["skill_id"]), {})[r["day"].isoformat()] = int(r["n"])
        return out

    async def usage_by_ids(
        self, session: AsyncSession, skill_ids: list[str]
    ) -> dict[str, dict]:
        """``{skill_id: {calls_30d, updated_at}}`` for enriching search hits."""
        if not skill_ids:
            return {}
        rows = (
            await session.execute(
                text(
                    "SELECT id, calls_30d, updated_at FROM skills "
                    "WHERE id = ANY(:ids)"
                ).bindparams(ids=skill_ids)
            )
        ).mappings().all()
        return {
            str(r["id"]): {"calls_30d": int(r["calls_30d"]), "updated_at": r["updated_at"]}
            for r in rows
        }

    async def stats(self, session: AsyncSession) -> dict[str, int]:
        """Counts by status + total 30-day calls, for the registry stat strip."""
        rows = (
            await session.execute(
                text(
                    "SELECT status, COUNT(*) AS n FROM skills "
                    "WHERE deleted_at IS NULL GROUP BY status"
                )
            )
        ).mappings().all()
        by_status = {r["status"]: int(r["n"]) for r in rows}
        calls = (
            await session.execute(
                text(
                    "SELECT COALESCE(SUM(calls_30d), 0) AS n FROM skills "
                    "WHERE deleted_at IS NULL"
                )
            )
        ).scalar_one()
        return {
            "stable": by_status.get("stable", 0),
            "active": by_status.get("active", 0),
            "review": by_status.get("review", 0),
            "draft": by_status.get("draft", 0),
            "total": sum(by_status.values()),
            "calls30d": int(calls),
        }

    async def insert_draft(
        self,
        session: AsyncSession,
        *,
        workspace_id: str,
        name: str,
        trigger: str | None,
        base_logic: str,
        description: str | None,
        embedding: list[float],
        embedding_model: str,
    ) -> str:
        """Insert a hand-authored skill at status ``draft`` (no source event).

        Human-authored, so ``source_authority='high'`` and ``confidence=1.0``;
        ``source_providers``/``source_ids`` stay empty. Returns the new skill id.
        Raises on the ``(workspace_id, name)`` unique constraint — the service maps
        that to a ``ConflictError``."""
        skill_id = generate_id("skill")
        await session.execute(
            text(
                """
                INSERT INTO skills
                    (id, workspace_id, name, status, source_providers, description,
                     trigger, base_logic, source_ids, source_authority, confidence,
                     embedding, embedding_model)
                VALUES
                    (:id, :ws, :name, CAST('draft' AS skill_status), '{}', :description,
                     :trigger, :base_logic, '[]'::jsonb, 'high', 1.0,
                     CAST(:embedding AS vector), :embedding_model)
                """
            ).bindparams(
                id=skill_id,
                ws=workspace_id,
                name=name,
                description=description,
                trigger=trigger,
                base_logic=base_logic,
                embedding=_vector_literal(embedding),
                embedding_model=embedding_model,
            )
        )
        return skill_id

    # ── embedding maintenance (re-embed job) ───────────────────────────────────

    async def list_stale_embeddings(
        self, session: AsyncSession, *, embedding_model: str, force: bool = False
    ) -> list[dict]:
        """Skills whose vector was not produced by ``embedding_model``.

        Stale = no vector yet, or one from a different model (``IS DISTINCT FROM``
        so a NULL provenance — a row written before migration 0018 stamped it —
        counts as stale rather than silently matching). ``force`` widens this to
        every non-deleted skill, for re-embedding after a change to the embedded
        text itself rather than to the model.

        Soft-deleted skills are excluded: their vectors are never searched
        (``similar_skills`` filters ``deleted_at IS NULL``), so re-embedding them
        would spend tokens on rows nothing can retrieve.
        """
        clause = (
            ""
            if force
            else " AND (embedding IS NULL OR embedding_model IS DISTINCT FROM :model)"
        )
        stmt = text(
            f"""
            SELECT id, trigger, base_logic
              FROM skills
             WHERE deleted_at IS NULL{clause}
             ORDER BY id
            """  # clause is a fixed literal; the model is bound
        )
        if not force:
            stmt = stmt.bindparams(model=embedding_model)
        rows = (await session.execute(stmt)).mappings().all()
        return [dict(r) for r in rows]

    async def set_embedding(
        self,
        session: AsyncSession,
        skill_id: str,
        *,
        embedding: list[float],
        embedding_model: str,
    ) -> None:
        """Replace a skill's vector and stamp which model produced it.

        Deliberately does **not** touch ``updated_at``: re-embedding is a storage
        migration, not an edit to the rule, and bumping the timestamp would make
        every skill look freshly changed in the dashboard and review surfaces.
        """
        await session.execute(
            text(
                """
                UPDATE skills
                   SET embedding = CAST(:embedding AS vector),
                       embedding_model = :model
                 WHERE id = :skill_id
                """
            ).bindparams(
                skill_id=skill_id,
                embedding=_vector_literal(embedding),
                model=embedding_model,
            )
        )

    async def decrement_confidence(
        self, session: AsyncSession, skill_id: str, *, amount: float, floor: float = 0.0
    ) -> float | None:
        """Lower a skill's stored confidence by ``amount`` (clamped at ``floor``).

        Returns the new confidence, or ``None`` if the skill is missing/deleted.
        The feedback loop's only skill mutation (PRD Feature 15a)."""
        row = (
            await session.execute(
                text(
                    """
                    UPDATE skills
                       SET confidence = GREATEST(:floor, COALESCE(confidence, 0) - :amount),
                           updated_at = now()
                     WHERE id = :id AND deleted_at IS NULL
                 RETURNING confidence
                    """
                ).bindparams(id=skill_id, amount=amount, floor=floor)
            )
        ).mappings().first()
        return float(row["confidence"]) if row else None

    # ── agent interactions (Feature 15/15a) ─────────────────────────────────────

    async def insert_interaction(
        self,
        session: AsyncSession,
        *,
        workspace_id: str,
        user_id: str | None,
        skill_id: str | None,
        query: str,
        matched_confidence: float | None,
        match_type: str,  # semantic | query_driven | no_match
        agent_action: dict | None = None,
    ) -> str:
        """Log one ``query_brain`` / search call. Returns the new interaction id."""
        row = (
            await session.execute(
                text(
                    """
                    INSERT INTO agent_interactions
                        (workspace_id, user_id, skill_id, query,
                         matched_confidence, match_type, agent_action)
                    VALUES
                        (:ws, :user_id, :skill_id, :query,
                         :matched_confidence, :match_type, CAST(:agent_action AS jsonb))
                 RETURNING id
                    """
                ).bindparams(
                    ws=workspace_id,
                    user_id=user_id,
                    skill_id=skill_id,
                    query=query,
                    matched_confidence=matched_confidence,
                    match_type=match_type,
                    agent_action=json.dumps(agent_action) if agent_action is not None else None,
                )
            )
        ).mappings().first()
        return str(row["id"])

    async def get_interaction(self, session: AsyncSession, interaction_id: str) -> dict | None:
        """Load one interaction (for the override feedback endpoint)."""
        row = (
            await session.execute(
                text(
                    """
                    SELECT id, workspace_id, skill_id, query, matched_confidence,
                           match_type, human_override
                      FROM agent_interactions
                     WHERE id = :id
                    """
                ).bindparams(id=interaction_id)
            )
        ).mappings().first()
        return dict(row) if row else None

    async def mark_interaction_override(
        self, session: AsyncSession, interaction_id: str
    ) -> None:
        """Flag an interaction as human-overridden (idempotent)."""
        await session.execute(
            text(
                "UPDATE agent_interactions SET human_override = TRUE WHERE id = :id"
            ).bindparams(id=interaction_id)
        )
