"""Data access for the brain chat surface (the only place its SQL lives).

Every method runs inside the caller's ``run_in_tenant`` transaction, so RLS scopes
each query to the workspace. Vector search itself is not here — the service reuses
the tested ``PipelineRepository.similar_skills`` HNSW query; this repository serves
the readiness count, the provenance dossier, per-skill citations, and chat
persistence.
"""
from __future__ import annotations

import json

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.skills.repository import PUBLISHED_STATUSES
from app.shared.helpers.ids import generate_id


class BrainRepository:
    async def count_indexed_skills(self, session: AsyncSession) -> int:
        """How many published skills carry an embedding (are retrievable).

        The Phase-0 readiness signal: chat is only "ready" once at least one
        reviewed skill is in the vector index. RLS scopes the count to the
        workspace; ``deleted_at IS NULL`` excludes soft-deleted skills.
        """
        row = (
            await session.execute(
                text(
                    """
                    SELECT COUNT(*) AS n FROM skills
                     WHERE status = ANY(:statuses)
                       AND embedding IS NOT NULL
                       AND deleted_at IS NULL
                    """
                ).bindparams(statuses=list(PUBLISHED_STATUSES))
            )
        ).first()
        return int(row.n) if row else 0

    # ── provenance & citations (governance answers, keyed by skill_id) ──────────

    async def provenance(self, session: AsyncSession, skill_id: str) -> dict:
        """Governance dossier for one skill — the "who/when/where it came from" a
        vector search can't surface and a synthesizer must never fabricate.

        Each sub-dict is ``None`` when the write path hasn't recorded it (e.g.
        ``skills.changed_by``/``skill_versions.changed_by`` are not yet populated by
        the pipeline, so ``createdBy``/``lastEditedBy`` names come back null). The
        service passes these through verbatim; unrecorded stays unrecorded.
        """
        approved_by = (
            await session.execute(
                text(
                    """
                    SELECT r.resolved_by AS id, u.name, r.resolved_at AS at
                      FROM reviews r
                      LEFT JOIN users u ON u.id = r.resolved_by
                     WHERE r.skill_id = :sid
                       AND r.verdict = 'approve'
                       AND r.resolved_by IS NOT NULL
                     ORDER BY r.resolved_at DESC NULLS LAST
                     LIMIT 1
                    """
                ).bindparams(sid=skill_id)
            )
        ).mappings().first()

        originated_by = (
            await session.execute(
                text(
                    """
                    SELECT evidence_author AS name, source_provider AS via,
                           source_location AS location
                      FROM reviews
                     WHERE skill_id = :sid AND evidence_author IS NOT NULL
                     ORDER BY created_at DESC
                     LIMIT 1
                    """
                ).bindparams(sid=skill_id)
            )
        ).mappings().first()

        created_by = (
            await session.execute(
                text(
                    """
                    SELECT sv.change_type, sv.changed_by AS id, u.name
                      FROM skill_versions sv
                      LEFT JOIN users u ON u.id = sv.changed_by
                     WHERE sv.skill_id = :sid
                     ORDER BY sv.created_at ASC
                     LIMIT 1
                    """
                ).bindparams(sid=skill_id)
            )
        ).mappings().first()

        last_edited_by = (
            await session.execute(
                text(
                    """
                    SELECT s.changed_by AS id, u.name, s.updated_at AS at
                      FROM skills s
                      LEFT JOIN users u ON u.id = s.changed_by
                     WHERE s.id = :sid AND s.deleted_at IS NULL
                    """
                ).bindparams(sid=skill_id)
            )
        ).mappings().first()

        return {
            "approved_by": (
                {"name": approved_by["name"], "at": approved_by["at"]}
                if approved_by else None
            ),
            "originated_by": (
                {
                    "name": originated_by["name"],
                    "via": originated_by["via"],
                    "location": originated_by["location"],
                }
                if originated_by else None
            ),
            # change_type is recorded even when changed_by (→ name) is not.
            "created_by": (
                {"name": created_by["name"], "change_type": created_by["change_type"]}
                if created_by and (created_by["name"] or created_by["change_type"])
                else None
            ),
            "last_edited_by": (
                {"name": last_edited_by["name"], "at": last_edited_by["at"]}
                if last_edited_by and last_edited_by["name"] else None
            ),
        }

    async def skill_citations(
        self, session: AsyncSession, skill_ids: list[str]
    ) -> dict[str, dict]:
        """``{skill_id: {provider, location}}`` for the cited skills.

        ``provider`` is the skill's first source provider; ``location`` is a
        best-effort channel/doc name from its most recent review evidence (``None``
        if none recorded). One round-trip for all cited skills.
        """
        if not skill_ids:
            return {}
        rows = (
            await session.execute(
                text(
                    """
                    SELECT s.id AS skill_id, s.source_providers,
                           (SELECT r.source_location FROM reviews r
                             WHERE r.skill_id = s.id AND r.source_location IS NOT NULL
                             ORDER BY r.created_at DESC LIMIT 1) AS location
                      FROM skills s
                     WHERE s.id = ANY(:ids) AND s.deleted_at IS NULL
                    """
                ).bindparams(ids=skill_ids)
            )
        ).mappings().all()
        out: dict[str, dict] = {}
        for r in rows:
            providers = r["source_providers"] or []
            out[r["skill_id"]] = {
                "provider": providers[0] if providers else None,
                "location": r["location"],
            }
        return out

    # ── chat persistence (dashboard JWT threads) ────────────────────────────────

    async def conversation_exists(self, session: AsyncSession, conversation_id: str) -> bool:
        """Whether the caller owns ``conversation_id`` (RLS restricts visibility to
        the caller's own conversations, so an unowned/absent id returns False)."""
        row = (
            await session.execute(
                text("SELECT 1 FROM brain_conversations WHERE id = :id").bindparams(
                    id=conversation_id
                )
            )
        ).first()
        return row is not None

    async def create_conversation(
        self, session: AsyncSession, *, workspace_id: str, user_id: str, title: str
    ) -> str:
        """Open a new conversation owned by ``user_id``. Returns the ``cnv_`` id."""
        conversation_id = generate_id("conversation")
        await session.execute(
            text(
                """
                INSERT INTO brain_conversations (id, workspace_id, user_id, title)
                VALUES (:id, :ws, :user_id, :title)
                """
            ).bindparams(
                id=conversation_id, ws=workspace_id, user_id=user_id, title=title
            )
        )
        return conversation_id

    async def touch_conversation(self, session: AsyncSession, conversation_id: str) -> None:
        """Bump ``updated_at`` so the sidebar sorts by recency."""
        await session.execute(
            text(
                "UPDATE brain_conversations SET updated_at = now() WHERE id = :id"
            ).bindparams(id=conversation_id)
        )

    async def insert_message(
        self,
        session: AsyncSession,
        *,
        workspace_id: str,
        conversation_id: str,
        role: str,  # user | assistant
        content: str,
        confidence: int | None = None,
        sources: list[dict] | None = None,
    ) -> str:
        """Append one turn (append-only; both turns written by the backend).

        ``sources`` follows the stored contract ``[{provider, label, sourceItemId,
        url, excerpt}]``. Returns the ``msg_`` id.
        """
        message_id = generate_id("message")
        await session.execute(
            text(
                """
                INSERT INTO brain_messages
                    (id, workspace_id, conversation_id, role, content, confidence, sources)
                VALUES
                    (:id, :ws, :cid, CAST(:role AS message_role), :content,
                     :confidence, CAST(:sources AS jsonb))
                """
            ).bindparams(
                id=message_id,
                ws=workspace_id,
                cid=conversation_id,
                role=role,
                content=content,
                confidence=confidence,
                sources=json.dumps(sources) if sources is not None else None,
            )
        )
        return message_id
