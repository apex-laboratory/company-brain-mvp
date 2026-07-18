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

# "Published" for delivery = agent-queryable states.
PUBLISHED_STATUSES: tuple[str, ...] = ("active", "stable")

_SKILL_COLUMNS = (
    "id, name, version, status, trigger, base_logic, exceptions_block, actions, "
    "source_authority, confidence, created_at, updated_at"
)


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
