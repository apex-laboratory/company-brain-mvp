"""Data access for the extraction pipeline (the only place pipeline SQL lives).

Same idiom as ``app/jobs/repository.py``: raw ``text()`` statements executed on
a session the caller opened inside ``run_in_tenant``, so RLS applies to the
worker. The pipeline holds NO session across LLM calls — the orchestrator loads
the event in one short transaction, runs the LLM stages connectionless, and
finalizes in a second transaction.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.shared.helpers.ids import generate_id


@dataclass(frozen=True)
class EventRow:
    """The ``source_events`` columns the pipeline needs."""

    id: str
    workspace_id: str
    provider: str
    event_type: str
    source_id: str | None
    external_event_id: str | None
    source_connection_id: str | None
    payload: dict
    processed: bool
    sweep_id: str | None
    attempts: int
    created_at: datetime


# Shared SELECT list + row→EventRow coercion so load_event and skill_provenance
# (which differ only in WHERE/ORDER BY) can't drift out of sync when a column is added.
_EVENT_COLUMNS = (
    "id, workspace_id, provider, event_type, source_id, external_event_id, "
    "source_connection_id, payload, processed, sweep_id, attempts, created_at"
)


def _to_event_row(row) -> EventRow:
    data = dict(row)
    data["id"] = str(data["id"])
    data["sweep_id"] = str(data["sweep_id"]) if data["sweep_id"] else None
    data["payload"] = data["payload"] or {}
    return EventRow(**data)


@dataclass(frozen=True)
class SimilarSkill:
    """A published/pending skill returned by pgvector search, with its cosine
    similarity to the query draft."""

    id: str
    name: str
    version: str
    base_logic: str
    exceptions_block: list
    source_authority: str | None
    similarity: float
    status: str | None = None
    # Usage columns projected by the same vector query so the search surface
    # doesn't have to re-SELECT the rows it just retrieved.
    calls_30d: int = 0
    updated_at: object | None = None


def _vector_literal(embedding: list[float]) -> str:
    """pgvector input literal: '[0.1,0.2,...]'."""
    return "[" + ",".join(repr(float(v)) for v in embedding) + "]"


class PipelineRepository:
    # ── events ────────────────────────────────────────────────────────────────

    async def load_event(self, session: AsyncSession, event_id: str) -> EventRow | None:
        row = (
            await session.execute(
                text(
                    f"SELECT {_EVENT_COLUMNS} FROM source_events WHERE id = CAST(:id AS uuid)"
                ).bindparams(id=event_id)
            )
        ).mappings().first()
        return _to_event_row(row) if row is not None else None

    async def finalize_event(
        self,
        session: AsyncSession,
        event_id: str,
        *,
        outcome: str,
        skill_id: str | None = None,
        pipeline_meta: dict | None = None,
    ) -> None:
        """Terminal write for one pipeline run: outcome + attempts increment.

        ``processed`` goes true on every terminal outcome including ``failed``;
        a re-run resets it to false + outcome='queued' first (retry-failed path).
        """
        await session.execute(
            text(
                """
                UPDATE source_events
                   SET processed = TRUE,
                       outcome = :outcome,
                       skill_id = COALESCE(:skill_id, skill_id),
                       pipeline_meta = CAST(:meta AS jsonb),
                       attempts = attempts + 1
                 WHERE id = CAST(:id AS uuid)
                """
            ).bindparams(
                id=event_id,
                outcome=outcome,
                skill_id=skill_id,
                meta=json.dumps(pipeline_meta or {}),
            )
        )

    async def skill_provenance(
        self, session: AsyncSession, skill_id: str
    ) -> EventRow | None:
        """Most recent source_event that produced ``skill_id`` (for a
        contradiction card's existing-source dict). ``None`` if none recorded."""
        row = (
            await session.execute(
                text(
                    f"""
                    SELECT {_EVENT_COLUMNS} FROM source_events
                     WHERE skill_id = :skill_id
                     ORDER BY created_at DESC
                     LIMIT 1
                    """
                ).bindparams(skill_id=skill_id)
            )
        ).mappings().first()
        return _to_event_row(row) if row is not None else None

    # ── skills ────────────────────────────────────────────────────────────────

    async def similar_skills(
        self,
        session: AsyncSession,
        workspace_id: str,
        embedding: list[float],
        statuses: tuple[str, ...],
        *,
        limit: int = 3,
    ) -> list[SimilarSkill]:
        """Top-``limit`` non-deleted skills by cosine similarity, restricted to
        ``statuses`` (published scope vs sweep scope). Uses the HNSW cosine index
        (``embedding <=> :vec``); similarity = ``1 - distance``.
        """
        rows = (
            await session.execute(
                text(
                    """
                    SELECT id, name, version, base_logic, exceptions_block,
                           source_authority, status, calls_30d, updated_at,
                           1 - (embedding <=> CAST(:vec AS vector)) AS similarity
                      FROM skills
                     WHERE workspace_id = :ws
                       AND status = ANY(:statuses)
                       AND embedding IS NOT NULL
                       AND deleted_at IS NULL
                     ORDER BY embedding <=> CAST(:vec AS vector)
                     LIMIT :limit
                    """
                ).bindparams(
                    ws=workspace_id,
                    vec=_vector_literal(embedding),
                    statuses=list(statuses),
                    limit=limit,
                )
            )
        ).mappings().all()
        return [
            SimilarSkill(
                id=r["id"],
                name=r["name"],
                version=r["version"],
                base_logic=r["base_logic"] or "",
                exceptions_block=r["exceptions_block"] or [],
                source_authority=r["source_authority"],
                similarity=float(r["similarity"]),
                status=r["status"],
                calls_30d=int(r["calls_30d"] or 0),
                updated_at=r["updated_at"],
            )
            for r in rows
        ]

    async def append_source_id(
        self, session: AsyncSession, skill_id: str, event_id: str
    ) -> None:
        """Record that ``event_id`` also supports ``skill_id`` (DUPLICATE path)."""
        await session.execute(
            text(
                """
                UPDATE skills
                   SET source_ids = source_ids || CAST(:eid AS jsonb),
                       updated_at = now()
                 WHERE id = :skill_id
                """
            ).bindparams(skill_id=skill_id, eid=json.dumps([event_id]))
        )

    async def update_skill_logic(
        self,
        session: AsyncSession,
        skill_id: str,
        *,
        base_logic: str,
        exceptions_block: list,
        version: str,
        confidence: float,
        embedding: list[float],
        embedding_model: str,
        status: str = "active",
        actions: list | None = None,
    ) -> None:
        """Apply an UPDATE/EXCEPTION to a published skill (auto-publish branch).

        ``actions`` is tri-state: a list replaces the column, ``None`` leaves it
        untouched. Callers that replace ``base_logic`` must pass the actions belonging
        to the *new* rule — see ``reviews.service._superseded_clauses``.
        """
        params: dict = {
            "skill_id": skill_id,
            "base_logic": base_logic,
            "exceptions": json.dumps(exceptions_block),
            "version": version,
            "confidence": confidence,
            "embedding": _vector_literal(embedding),
            "embedding_model": embedding_model,
            "status": status,
        }
        # Fixed literal fragment chosen by a bool — no caller data reaches the SQL
        # text; the value itself stays parameter-bound like every other column.
        actions_set = ""
        if actions is not None:
            actions_set = "actions = CAST(:actions AS jsonb),\n                       "
            params["actions"] = json.dumps(actions)
        await session.execute(
            text(
                f"""
                UPDATE skills
                   SET base_logic = :base_logic,
                       exceptions_block = CAST(:exceptions AS jsonb),
                       {actions_set}version = :version,
                       confidence = :confidence,
                       embedding = CAST(:embedding AS vector),
                       embedding_model = :embedding_model,
                       status = CAST(:status AS skill_status),
                       updated_at = now()
                 WHERE id = :skill_id
                """
            ).bindparams(**params)
        )

    async def apply_exception(
        self,
        session: AsyncSession,
        skill_id: str,
        *,
        exceptions_block: list,
        version: str,
        confidence: float,
        status: str = "active",
    ) -> None:
        """Append-exception UPDATE: base_logic + embedding stay put (the rule is
        unchanged; only its carve-outs grow), so we don't touch the vector."""
        await session.execute(
            text(
                """
                UPDATE skills
                   SET exceptions_block = CAST(:exceptions AS jsonb),
                       version = :version,
                       confidence = :confidence,
                       status = CAST(:status AS skill_status),
                       updated_at = now()
                 WHERE id = :skill_id
                """
            ).bindparams(
                skill_id=skill_id,
                exceptions=json.dumps(exceptions_block),
                version=version,
                confidence=confidence,
                status=status,
            )
        )

    async def get_skill(self, session: AsyncSession, skill_id: str) -> dict | None:
        """Current skill fields needed to apply a review approval."""
        row = (
            await session.execute(
                text(
                    """
                    SELECT id, workspace_id, name, status, version, trigger,
                           base_logic, exceptions_block, confidence
                      FROM skills
                     WHERE id = :id AND deleted_at IS NULL
                    """
                ).bindparams(id=skill_id)
            )
        ).mappings().first()
        return dict(row) if row else None

    async def set_skill_status(
        self,
        session: AsyncSession,
        skill_id: str,
        status: str,
        *,
        confidence: float | None = None,
    ) -> None:
        """Set a skill's status, optionally raising its stored confidence too.

        Human approval of a new_decision must propagate 1.0 to ``skills.confidence``
        (like the policy_change/exception branches) so downstream confidence
        thresholds don't treat an approved skill as low-confidence. ``NULL``
        confidence leaves the column untouched (the reject→draft demotion path)."""
        await session.execute(
            text(
                """
                UPDATE skills
                   SET status = CAST(:status AS skill_status),
                       confidence = COALESCE(:confidence, confidence),
                       updated_at = now()
                 WHERE id = :skill_id
                """
            ).bindparams(skill_id=skill_id, status=status, confidence=confidence)
        )

    async def resolve_skill_name(
        self, session: AsyncSession, workspace_id: str, name: str
    ) -> str:
        """Dedupe against UNIQUE(workspace_id, name) with a ' (n)' suffix."""
        taken = set(
            (
                await session.execute(
                    text(
                        """
                        SELECT name FROM skills
                         WHERE workspace_id = :ws
                           AND (name = :name OR name LIKE :like)
                        """
                    ).bindparams(ws=workspace_id, name=name, like=f"{name} (%")
                )
            ).scalars()
        )
        if name not in taken:
            return name
        n = 2
        while f"{name} ({n})" in taken:
            n += 1
        return f"{name} ({n})"

    async def insert_skill(
        self,
        session: AsyncSession,
        *,
        workspace_id: str,
        name: str,
        status: str,  # active | review | draft
        provider: str,
        trigger: str,
        base_logic: str,
        description: str | None,
        exceptions_block: list,
        actions: list,
        source_event_id: str,
        source_authority: str,
        confidence: float,
        embedding: list[float],
        embedding_model: str,
    ) -> str:
        skill_id = generate_id("skill")
        await session.execute(
            text(
                """
                INSERT INTO skills
                    (id, workspace_id, name, status, source_providers, description,
                     trigger, base_logic, exceptions_block, actions, source_ids,
                     source_authority, confidence, embedding, embedding_model)
                VALUES
                    (:id, :ws, :name, CAST(:status AS skill_status),
                     ARRAY[CAST(:provider AS text)], :description,
                     :trigger, :base_logic, CAST(:exceptions AS jsonb),
                     CAST(:actions AS jsonb), CAST(:source_ids AS jsonb),
                     :authority, :confidence, CAST(:embedding AS vector),
                     :embedding_model)
                """
            ).bindparams(
                id=skill_id,
                ws=workspace_id,
                name=name,
                status=status,
                provider=provider,
                description=description,
                trigger=trigger,
                base_logic=base_logic,
                exceptions=json.dumps(exceptions_block),
                actions=json.dumps(actions),
                source_ids=json.dumps([source_event_id]),
                authority=source_authority,
                confidence=confidence,
                embedding=_vector_literal(embedding),
                embedding_model=embedding_model,
            )
        )
        return skill_id

    async def insert_skill_version(
        self,
        session: AsyncSession,
        *,
        workspace_id: str,
        skill_id: str,
        version: str,
        base_logic: str,
        exceptions_block: list,
        confidence: float,
        change_type: str,  # create | update | human_edit | sweep_sourced
        description: str | None = None,
    ) -> str:
        version_id = generate_id("skill_version")
        await session.execute(
            text(
                """
                INSERT INTO skill_versions
                    (id, workspace_id, skill_id, version, description,
                     base_logic, exceptions_block, confidence, change_type)
                VALUES
                    (:id, :ws, :skill_id, :version, :description,
                     :base_logic, CAST(:exceptions AS jsonb), :confidence, :change_type)
                """
            ).bindparams(
                id=version_id,
                ws=workspace_id,
                skill_id=skill_id,
                version=version,
                description=description,
                base_logic=base_logic,
                exceptions=json.dumps(exceptions_block),
                confidence=confidence,
                change_type=change_type,
            )
        )
        return version_id

    # ── reviews ───────────────────────────────────────────────────────────────

    async def insert_review(
        self,
        session: AsyncSession,
        *,
        workspace_id: str,
        title: str,
        kind: str,  # policy_change | new_decision | contradiction | exception
        provider: str,
        source_location: str | None,
        before_text: str | None,
        after_text: str | None,
        evidence_quote: str | None,
        evidence_author: str | None,
        confidence: int,  # 0–100 (reviews scale)
        skill_id: str | None,
        payload: dict | None,
    ) -> str:
        review_id = generate_id("review")
        await session.execute(
            text(
                """
                INSERT INTO reviews
                    (id, workspace_id, title, kind, source_provider, source_location,
                     before_text, after_text, evidence_quote, evidence_author,
                     confidence, payload, skill_id)
                VALUES
                    (:id, :ws, :title, CAST(:kind AS review_kind),
                     CAST(:provider AS source_provider), :source_location,
                     :before_text, :after_text, :evidence_quote, :evidence_author,
                     :confidence, CAST(:payload AS jsonb), :skill_id)
                """
            ).bindparams(
                id=review_id,
                ws=workspace_id,
                title=title,
                kind=kind,
                provider=provider,
                source_location=source_location,
                before_text=before_text,
                after_text=after_text,
                evidence_quote=evidence_quote,
                evidence_author=evidence_author,
                confidence=confidence,
                payload=json.dumps(payload) if payload is not None else None,
                skill_id=skill_id,
            )
        )
        return review_id

    # ── sweep extraction ──────────────────────────────────────────────────────

    async def list_sweep_queued_events(
        self, session: AsyncSession, sweep_id: str, *, limit: int | None = None
    ) -> list[tuple[str, str]]:
        """``(event_id, provider)`` for a sweep's un-extracted events, oldest first.

        The caller re-orders by provider authority rank; ``created_at`` breaks ties
        within a provider. Re-selects on every (re)entry, so a crashed sweep_extract
        resumes exactly where it left off — finalized events drop out of the set.
        ``limit`` bounds one invocation's work so a huge historical sweep can't
        exceed the job timeout during the paced launch loop (the caller re-enqueues
        a continuation while any remain).
        """
        rows = (
            await session.execute(
                text(
                    """
                    SELECT id, provider FROM source_events
                     WHERE sweep_id = CAST(:sweep_id AS uuid)
                       AND processed = FALSE
                       AND outcome = 'queued'
                     ORDER BY created_at
                     LIMIT :limit
                    """
                ).bindparams(sweep_id=sweep_id, limit=limit)
            )
        ).all()
        return [(str(r.id), r.provider) for r in rows]

    async def count_sweep_queued_events(
        self, session: AsyncSession, sweep_id: str
    ) -> int:
        """How many of a sweep's events are still ``queued`` (drives the
        continuation decision + the ``queued_remaining`` progress figure)."""
        row = (
            await session.execute(
                text(
                    """
                    SELECT COUNT(*) AS n FROM source_events
                     WHERE sweep_id = CAST(:sweep_id AS uuid)
                       AND processed = FALSE
                       AND outcome = 'queued'
                    """
                ).bindparams(sweep_id=sweep_id)
            )
        ).first()
        return int(row.n) if row else 0

    async def write_extraction_progress(
        self, session: AsyncSession, sweep_id: str, progress: dict
    ) -> None:
        """Write the extraction rollup into ``sweeps.progress['extraction']``.

        Delegates to the shared per-provider progress writer (keyed ``extraction``)
        so the ``jsonb_set`` UPDATE lives in exactly one place."""
        from app.jobs.repository import JobsRepository

        await JobsRepository().update_sweep_source_progress(
            session, sweep_id, "extraction", progress
        )

    # ── sweep counters ────────────────────────────────────────────────────────

    async def bump_sweep_counters(
        self,
        session: AsyncSession,
        sweep_id: str,
        *,
        created: int = 0,
        queued: int = 0,
    ) -> None:
        await session.execute(
            text(
                """
                UPDATE sweeps
                   SET skills_created = skills_created + :created,
                       skills_queued = skills_queued + :queued
                 WHERE id = CAST(:id AS uuid)
                """
            ).bindparams(id=sweep_id, created=created, queued=queued)
        )
