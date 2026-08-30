"""Data access for agent-run ingestion (the only place its SQL lives).

Every method runs inside the caller's ``run_in_tenant`` transaction, so RLS scopes
each statement to the workspace; ``workspace_id`` is also bound explicitly on
writes (defense in depth, per the house rule).

Two things here are load-bearing beyond ordinary CRUD:

* ``insert`` is an UPSERT on ``(workspace_id, external_id)``. A hook shim that
  crashes mid-flush re-sends the same envelope, and a second row would let one
  task attempt count twice toward ``min_runs_per_cluster`` — inflating an anecdote
  into "evidence" without a second real run behind it.
* ``nearest_cluster`` searches only *undistilled* runs of the same agent. Matching
  against already-distilled runs would re-open a settled cluster every time the
  agent repeated a task it already has a procedure for.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def _vector_literal(embedding: list[float]) -> str:
    """pgvector input literal: '[0.1,0.2,...]' (mirrors the pipeline/skills helper)."""
    return "[" + ",".join(repr(float(v)) for v in embedding) + "]"


def _parse_vector(value: Any) -> list[float] | None:
    """Read a pgvector column back into floats.

    No pgvector codec is registered on the asyncpg connection, so a ``vector``
    column arrives as its **text literal** — ``'[0.1,0.2,...]'`` — not as a
    sequence. Handing that straight to ``list()`` yields a list of *characters*,
    and the first float conversion downstream dies on ``'['``. Parsing at the
    repository boundary keeps the driver's representation from leaking into the
    gate, which is the only place that reads this column into Python.
    """
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip().strip("[]")
        if not stripped:
            return []
        return [float(part) for part in stripped.split(",")]
    return [float(v) for v in value]


class RunsRepository:
    async def insert(
        self,
        session: AsyncSession,
        *,
        run_id: str,
        workspace_id: str,
        external_id: str | None,
        agent_name: str,
        task: str,
        task_embedding: list[float] | None,
        outcome: str,
        outcome_signals: dict[str, Any],
        trace: list[dict[str, Any]] | None,
        trace_digest: dict[str, Any],
        step_count: int,
        tokens_used: int | None,
        duration_ms: int | None,
        harness: str,
        ingest_mode: str,
        eligible: bool | None,
        ineligible_reason: str | None,
    ) -> tuple[str, bool]:
        """Insert (or idempotently update) a run. Returns ``(run_id, was_duplicate)``.

        On conflict the *newer* payload wins: a re-push usually carries a more
        complete trace than the one that raced it (the flush that crashed had
        fewer steps). Gate columns are reset to NULL so the run is judged again
        against the fuller evidence — but only when it has not already been
        distilled, since re-gating a run whose procedure is already in review
        would let it contribute to a second cluster.
        """
        embedding_sql = "CAST(:task_embedding AS vector)" if task_embedding else "NULL"
        row = (
            await session.execute(
                text(
                    f"""
                    INSERT INTO agent_runs (
                        id, workspace_id, external_id, agent_name, task, task_embedding,
                        outcome, outcome_signals, trace, trace_digest, step_count,
                        tokens_used, duration_ms, harness, ingest_mode,
                        eligible, ineligible_reason
                    ) VALUES (
                        :id, :workspace_id, :external_id, :agent_name, :task,
                        {embedding_sql},
                        :outcome, CAST(:outcome_signals AS jsonb),
                        CAST(:trace AS jsonb), CAST(:trace_digest AS jsonb), :step_count,
                        :tokens_used, :duration_ms, :harness, :ingest_mode,
                        :eligible, :ineligible_reason
                    )
                    ON CONFLICT (workspace_id, external_id) DO UPDATE SET
                        agent_name        = EXCLUDED.agent_name,
                        task              = EXCLUDED.task,
                        task_embedding    = EXCLUDED.task_embedding,
                        outcome           = EXCLUDED.outcome,
                        outcome_signals   = EXCLUDED.outcome_signals,
                        trace             = EXCLUDED.trace,
                        trace_digest      = EXCLUDED.trace_digest,
                        step_count        = EXCLUDED.step_count,
                        tokens_used       = EXCLUDED.tokens_used,
                        duration_ms       = EXCLUDED.duration_ms,
                        harness           = EXCLUDED.harness,
                        eligible          = EXCLUDED.eligible,
                        ineligible_reason = EXCLUDED.ineligible_reason,
                        cluster_id        = NULL
                      WHERE agent_runs.distilled_at IS NULL
                    RETURNING id, (xmax <> 0) AS was_update
                    """
                ),
                {
                    "id": run_id,
                    "workspace_id": workspace_id,
                    "external_id": external_id,
                    "agent_name": agent_name,
                    "task": task,
                    "task_embedding": (
                        _vector_literal(task_embedding) if task_embedding else None
                    ),
                    "outcome": outcome,
                    "outcome_signals": json.dumps(outcome_signals),
                    "trace": json.dumps(trace) if trace is not None else None,
                    "trace_digest": json.dumps(trace_digest),
                    "step_count": step_count,
                    "tokens_used": tokens_used,
                    "duration_ms": duration_ms,
                    "harness": harness,
                    "ingest_mode": ingest_mode,
                    "eligible": eligible,
                    "ineligible_reason": ineligible_reason,
                },
            )
        ).first()

        if row is None:
            # DO UPDATE ... WHERE was filtered out: the row exists and is already
            # distilled. Nothing to change — report it as a duplicate so a retrying
            # harness stops, and resolve the caller-facing id from the existing row.
            existing = (
                await session.execute(
                    text(
                        "SELECT id FROM agent_runs "
                        " WHERE workspace_id = :workspace_id AND external_id = :external_id"
                    ),
                    {"workspace_id": workspace_id, "external_id": external_id},
                )
            ).first()
            return (existing[0] if existing else run_id), True

        return row[0], bool(row[1])

    async def get_for_gate(
        self, session: AsyncSession, run_id: str
    ) -> dict[str, Any] | None:
        """Load the columns the success gate needs.

        Includes ``trace`` because the gate's tail check reads step statuses, and
        ``task_embedding`` because clustering runs in the same transaction — one
        round-trip for the whole verdict rather than three."""
        row = (
            await session.execute(
                text(
                    """
                    SELECT id, agent_name, task, task_embedding, outcome,
                           outcome_signals, trace, trace_digest, step_count,
                           eligible, distilled_at
                      FROM agent_runs
                     WHERE id = :run_id
                    """
                ),
                {"run_id": run_id},
            )
        ).mappings().first()
        if row is None:
            return None
        out = dict(row)
        out["task_embedding"] = _parse_vector(out.get("task_embedding"))
        return out

    async def mark_gated(
        self,
        session: AsyncSession,
        *,
        run_id: str,
        eligible: bool,
        ineligible_reason: str | None,
        cluster_id: str | None,
    ) -> None:
        """Record the gate verdict. ``cluster_id`` is set only for eligible runs."""
        await session.execute(
            text(
                """
                UPDATE agent_runs
                   SET eligible = :eligible,
                       ineligible_reason = :ineligible_reason,
                       cluster_id = :cluster_id
                 WHERE id = :run_id
                """
            ),
            {
                "run_id": run_id,
                "eligible": eligible,
                "ineligible_reason": ineligible_reason,
                "cluster_id": cluster_id,
            },
        )

    async def duplicate_digest_exists(
        self, session: AsyncSession, *, run_id: str, agent_name: str, sha: str
    ) -> bool:
        """True if another run of the same agent already carries this trace digest.

        Catches the harness that replays an identical trace (a retried flush that
        lost its ``external_id``, or a scripted run repeated verbatim). Identical
        traces are one piece of evidence, not N — counting them N times is exactly
        how ``min_runs_per_cluster`` gets gamed by accident.
        """
        row = (
            await session.execute(
                text(
                    """
                    SELECT 1 FROM agent_runs
                     WHERE id <> :run_id
                       AND agent_name = :agent_name
                       AND trace_digest ->> 'sha256' = :sha
                     LIMIT 1
                    """
                ),
                {"run_id": run_id, "agent_name": agent_name, "sha": sha},
            )
        ).first()
        return row is not None

    async def nearest_cluster(
        self,
        session: AsyncSession,
        *,
        run_id: str,
        agent_name: str,
        embedding: list[float],
        threshold: float,
    ) -> tuple[str | None, int]:
        """Find the task cluster this run belongs to.

        Returns ``(cluster_id, member_count)`` for the nearest eligible, undistilled
        run of the same agent within ``threshold`` cosine similarity — or
        ``(None, 0)`` when this run starts a new cluster.
        """
        row = (
            await session.execute(
                text(
                    """
                    SELECT cluster_id,
                           1 - (task_embedding <=> CAST(:embedding AS vector)) AS similarity
                      FROM agent_runs
                     WHERE id <> :run_id
                       AND agent_name = :agent_name
                       AND eligible IS TRUE
                       AND distilled_at IS NULL
                       AND cluster_id IS NOT NULL
                       AND task_embedding IS NOT NULL
                     ORDER BY task_embedding <=> CAST(:embedding AS vector)
                     LIMIT 1
                    """
                ),
                {
                    "run_id": run_id,
                    "agent_name": agent_name,
                    "embedding": _vector_literal(embedding),
                },
            )
        ).first()

        if row is None or row[1] is None or float(row[1]) < threshold:
            return None, 0

        cluster_id = row[0]
        count = (
            await session.execute(
                text(
                    """
                    SELECT count(*) FROM agent_runs
                     WHERE cluster_id = :cluster_id
                       AND eligible IS TRUE
                       AND distilled_at IS NULL
                    """
                ),
                {"cluster_id": cluster_id},
            )
        ).scalar_one()
        return cluster_id, int(count)
