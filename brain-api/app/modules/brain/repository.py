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


def _vector_literal(embedding: list[float]) -> str:
    """pgvector input literal: '[0.1,0.2,...]' (mirrors pipeline.repository)."""
    return "[" + ",".join(repr(float(v)) for v in embedding) + "]"


def chunk_key(kind: str, skill_id: str, ref: str, chunk_index: int) -> str:
    """Deterministic idempotency key for a chunk (unique per workspace).

    ``ref`` is the version (``skill_version`` kind) or the source item id
    (``evidence`` kind). The backfill upserts on this, so a re-run of the same
    (skill, version/source, chunk) is a no-op rather than a duplicate row.
    """
    return f"{kind}:{skill_id}:{ref}:{chunk_index}"


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

    # ── brain_chunks index (Phase 2: skill versions) ────────────────────────────

    async def list_version_index_rows(self, session: AsyncSession) -> list[dict]:
        """Every (skill, version) body to index: all ``skill_versions`` of published
        skills, plus any published skill that has no version row yet.

        ``is_current`` = the version equals the skill's live ``version``. RLS scopes
        both tables to the workspace.
        """
        rows = (
            await session.execute(
                text(
                    """
                    SELECT sv.skill_id, sv.version, sv.base_logic,
                           (sv.version = s.version) AS is_current
                      FROM skill_versions sv
                      JOIN skills s ON s.id = sv.skill_id
                     WHERE s.deleted_at IS NULL AND s.status = ANY(:statuses)
                    UNION ALL
                    SELECT s.id, s.version, s.base_logic, TRUE
                      FROM skills s
                     WHERE s.deleted_at IS NULL AND s.status = ANY(:statuses)
                       AND NOT EXISTS (
                         SELECT 1 FROM skill_versions sv WHERE sv.skill_id = s.id
                       )
                    """
                ).bindparams(statuses=list(PUBLISHED_STATUSES))
            )
        ).mappings().all()
        return [dict(r) for r in rows]

    async def list_chunk_keys(
        self, session: AsyncSession, *, kind: str | None = None
    ) -> set[str]:
        """Already-indexed ``chunk_key``s (of ``kind``, or all kinds when ``None``) —
        the backfill skips these so a re-run only embeds what's new (idempotency)."""
        clause = " WHERE kind = :kind" if kind is not None else ""
        stmt = text(f"SELECT chunk_key FROM brain_chunks{clause}")
        if kind is not None:
            stmt = stmt.bindparams(kind=kind)
        rows = (await session.execute(stmt)).scalars()
        return set(rows)

    async def upsert_skill_version_chunk(
        self,
        session: AsyncSession,
        *,
        workspace_id: str,
        skill_id: str,
        version: str,
        is_current: bool,
        chunk_index: int,
        chunk_key: str,
        content: str,
        embedding: list[float],
    ) -> None:
        """Insert or refresh one ``skill_version`` chunk (idempotent on chunk_key)."""
        await session.execute(
            text(
                """
                INSERT INTO brain_chunks
                    (workspace_id, kind, skill_id, version, is_current,
                     chunk_index, chunk_key, content, embedding)
                VALUES
                    (:ws, 'skill_version', :skill_id, :version, :is_current,
                     :idx, :key, :content, CAST(:embedding AS vector))
                ON CONFLICT (workspace_id, chunk_key) DO UPDATE
                   SET content = EXCLUDED.content,
                       embedding = EXCLUDED.embedding,
                       is_current = EXCLUDED.is_current,
                       version = EXCLUDED.version
                """
            ).bindparams(
                ws=workspace_id,
                skill_id=skill_id,
                version=version,
                is_current=is_current,
                idx=chunk_index,
                key=chunk_key,
                content=content,
                embedding=_vector_literal(embedding),
            )
        )

    async def sync_skill_version_current(self, session: AsyncSession) -> None:
        """Recompute ``is_current`` for every ``skill_version`` chunk from the skill's
        live version — promotes the new current and demotes the prior one in a single
        set-based pass (the "demote prior version" mechanism, kept idempotent)."""
        await session.execute(
            text(
                """
                UPDATE brain_chunks bc
                   SET is_current = (bc.version = s.version)
                  FROM skills s
                 WHERE bc.skill_id = s.id
                   AND bc.kind = 'skill_version'
                   AND bc.is_current IS DISTINCT FROM (bc.version = s.version)
                """
            )
        )

    async def version_history(
        self, session: AsyncSession, skill_ids: list[str]
    ) -> list[dict]:
        """Superseded version bodies for the matched skills — the "what did it used
        to be / when did it change" context, always labeled superseded so the
        synthesizer never states an old rule as current."""
        if not skill_ids:
            return []
        rows = (
            await session.execute(
                text(
                    """
                    SELECT skill_id, version, content
                      FROM brain_chunks
                     WHERE skill_id = ANY(:ids)
                       AND kind = 'skill_version'
                       AND is_current = FALSE
                     ORDER BY skill_id, version
                    """
                ).bindparams(ids=skill_ids)
            )
        ).mappings().all()
        return [dict(r) for r in rows]

    async def count_chunks(self, session: AsyncSession, *, kind: str | None = None) -> int:
        """How many chunks are indexed (observability / readiness signal)."""
        clause = " WHERE kind = :kind" if kind is not None else ""
        stmt = text(f"SELECT COUNT(*) AS n FROM brain_chunks{clause}")
        if kind is not None:
            stmt = stmt.bindparams(kind=kind)
        row = (await session.execute(stmt)).first()
        return int(row.n) if row else 0

    # ── brain_chunks index (Phase 3: skill evidence graph) ──────────────────────

    async def list_evidence_rows(self, session: AsyncSession) -> list[dict]:
        """Each published skill's durable evidence — the deciding messages that fed
        it (``reviews.evidence_quote`` + author + provider + channel/doc).

        This is the retrievable "whose message said we need this" node: the review
        curated it at extraction, so it's the highest-signal source material without
        re-touching the extraction hot path. (Fuller source spans from
        ``source_events.payload`` are a later enrichment.)
        """
        rows = (
            await session.execute(
                text(
                    """
                    SELECT r.skill_id, r.id AS review_id,
                           r.source_provider AS provider, r.source_location AS location,
                           r.evidence_author AS author, r.evidence_quote AS content
                      FROM reviews r
                      JOIN skills s ON s.id = r.skill_id
                     WHERE s.deleted_at IS NULL AND s.status = ANY(:statuses)
                       AND r.evidence_quote IS NOT NULL AND r.skill_id IS NOT NULL
                    """
                ).bindparams(statuses=list(PUBLISHED_STATUSES))
            )
        ).mappings().all()
        return [dict(r) for r in rows]

    async def upsert_evidence_chunk(
        self,
        session: AsyncSession,
        *,
        workspace_id: str,
        skill_id: str,
        source_ref: dict,
        chunk_index: int,
        chunk_key: str,
        content: str,
        embedding: list[float],
    ) -> None:
        """Insert or refresh one ``evidence`` chunk (idempotent on chunk_key).

        ``source_ref`` = {provider, sourceItemId, url, label, author} — the citation
        the FE renders and the synthesizer attributes the quote to.
        """
        await session.execute(
            text(
                """
                INSERT INTO brain_chunks
                    (workspace_id, kind, skill_id, source_ref,
                     chunk_index, chunk_key, content, embedding)
                VALUES
                    (:ws, 'evidence', :skill_id, CAST(:source_ref AS jsonb),
                     :idx, :key, :content, CAST(:embedding AS vector))
                ON CONFLICT (workspace_id, chunk_key) DO UPDATE
                   SET content = EXCLUDED.content,
                       embedding = EXCLUDED.embedding,
                       source_ref = EXCLUDED.source_ref
                """
            ).bindparams(
                ws=workspace_id,
                skill_id=skill_id,
                source_ref=json.dumps(source_ref),
                idx=chunk_index,
                key=chunk_key,
                content=content,
                embedding=_vector_literal(embedding),
            )
        )

    async def similar_chunks(
        self,
        session: AsyncSession,
        workspace_id: str,
        embedding: list[float],
        *,
        kind: str,
        limit: int = 3,
    ) -> list[dict]:
        """Top-``limit`` chunks of ``kind`` by cosine similarity (HNSW), restricted
        to chunks of **non-deleted** skills — so a soft-deleted skill's evidence or
        history is never served even before the row is hard-deleted. Used for the
        Phase-3 evidence facet of retrieval.
        """
        rows = (
            await session.execute(
                text(
                    """
                    SELECT bc.skill_id, bc.kind, bc.version, bc.source_ref, bc.content,
                           1 - (bc.embedding <=> CAST(:vec AS vector)) AS similarity
                      FROM brain_chunks bc
                      JOIN skills s ON s.id = bc.skill_id
                     WHERE bc.workspace_id = :ws
                       AND bc.kind = :kind
                       AND bc.embedding IS NOT NULL
                       AND s.deleted_at IS NULL
                     ORDER BY bc.embedding <=> CAST(:vec AS vector)
                     LIMIT :limit
                    """
                ).bindparams(
                    ws=workspace_id,
                    vec=_vector_literal(embedding),
                    kind=kind,
                    limit=limit,
                )
            )
        ).mappings().all()
        return [dict(r) for r in rows]
