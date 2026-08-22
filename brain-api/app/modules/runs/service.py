"""Agent-run ingestion business logic (PRD Feature 29, Process 8 front half).

Ingest does four things before the row is written, in this order: **redact**,
**normalize + digest**, **embed the task**, **enqueue the gate** — then returns
``202`` immediately. Everything expensive happens on the worker.

Two constraints shape the whole module:

* **Network I/O never happens inside a transaction.** The task embedding is
  computed before the tenant session opens, so an OpenAI call can never pin a
  pooled connection (the orchestrator/reviews rule, BEST_PRACTICES §7).
* **Ingestion never blocks the agent's critical path.** A run push must cost the
  caller a single fast round-trip, because the caller is a hook running inside
  somebody's editor. That is why gating is enqueued rather than awaited, and why
  a queue outage degrades to "gated later" instead of a 500.

A run whose redaction fails is still stored — with ``eligible = false``,
``ineligible_reason = 'redaction_failed'`` and **no trace body**. Dropping the row
entirely would lose a real product signal (PRD §15 tracks failure rates per agent),
and storing the body "just in case" is the thing redaction exists to prevent.
"""
from __future__ import annotations

import logging
from collections import Counter
from typing import Any

from app.config.database import get_tenant_session
from app.config.settings import settings
from app.jobs.queue import enqueue
from app.modules.runs.redaction import RedactionFailed, digest_of, redact_steps, scrub_text
from app.modules.runs.repository import RunsRepository
from app.modules.runs.schemas import RunAccepted, RunIngestRequest
from app.pipeline import embedder
from app.shared.helpers.ids import generate_id
from app.shared.middleware.authenticate import AuthContext
from app.shared.middleware.with_tenant import run_in_tenant

log = logging.getLogger(__name__)

# Step types that name a file, for the digest's files_touched set.
_FILE_STEPS = frozenset({"file_read", "file_write"})


def _require_workspace(auth: AuthContext) -> tuple[str, str]:
    if auth.workspace_id is None or auth.role is None:
        raise RuntimeError(
            "BUG: runs service called without workspace context — "
            "ensure require_scope('runs:write') is declared on this route"
        )
    return auth.workspace_id, auth.role


def build_digest(steps: list[dict[str, Any]], req: RunIngestRequest) -> dict[str, Any]:
    """The durable summary that outlives the raw trace.

    Once the retention job NULLs ``trace``, this is everything left. It has to
    answer the questions the product asks after the fact — how long was this run,
    what did it touch, which tools did it lean on, is it the same trace as that
    other one — without retaining a byte of content.
    """
    tool_histogram = Counter(
        step.get("name") or step.get("type", "unknown")
        for step in steps
        if step.get("type") == "tool_call"
    )
    files_touched = sorted({
        str(step.get("name"))
        for step in steps
        if step.get("type") in _FILE_STEPS and step.get("name")
    })
    error_steps = sum(1 for step in steps if step.get("status") == "error")
    return {
        "sha256": digest_of(steps)["sha256"],
        "step_count": len(steps),
        "error_steps": error_steps,
        "tool_histogram": dict(tool_histogram),
        # Capped: a run that touched 4,000 files would otherwise make the digest
        # bigger than the trace it replaces.
        "files_touched": files_touched[:100],
        "files_touched_count": len(files_touched),
        "type_histogram": dict(Counter(str(step.get("type")) for step in steps)),
        "duration_ms": req.duration_ms,
        "tokens_used": req.tokens_used,
        "harness": req.harness,
    }


class RunsService:
    def __init__(self, repository: RunsRepository | None = None) -> None:
        self._repo = repository or RunsRepository()

    async def ingest(self, auth: AuthContext, req: RunIngestRequest) -> RunAccepted:
        """Redact, digest, embed, store, enqueue the gate. Returns a ``202`` receipt."""
        workspace_id, role = _require_workspace(auth)

        # ── phase 1: no transaction open ─────────────────────────────────────
        # The task string is free text a human typed into an editor; it gets the
        # same scrub as the steps. A pasted credential in the *prompt* is at least
        # as likely as one in tool output, and this one also reaches the embedder.
        task = scrub_text(req.task)
        agent_name = scrub_text(req.agent_name)

        steps: list[dict[str, Any]] | None
        eligible: bool | None = None
        ineligible_reason: str | None = None
        try:
            steps = redact_steps(
                [s.model_dump(exclude_none=True) for s in req.steps],
                max_payload_bytes=settings.run_step_payload_max_bytes,
            )
        except RedactionFailed:
            # Fail closed: keep the run as a signal, keep none of its content.
            log.warning(
                "run ingest: redaction failed for workspace=%s agent=%s — trace dropped",
                workspace_id, agent_name,
            )
            steps = None
            eligible, ineligible_reason = False, "redaction_failed"

        digest = build_digest(steps, req) if steps is not None else {}

        # Only embed what can still be clustered. An already-rejected run will
        # never join a cluster, so the embedding call would be pure spend.
        embedding: list[float] | None = None
        if steps is not None:
            try:
                embedding, _ = await embedder.embed_text(task, interactive=True)
            except Exception:  # noqa: BLE001 — ingest must not fail on an embed outage
                # Storing the run un-embedded is recoverable: the gate marks it
                # ungated-and-unclustered, and a re-push or backfill can embed it
                # later. Rejecting the push is not — the trace is gone from the
                # client's spool the moment we 2xx or 4xx it.
                log.exception("run ingest: task embedding failed; storing unclustered")

        run_id = generate_id("agent_run")

        # ── phase 2: transaction ─────────────────────────────────────────────
        async with get_tenant_session() as session, run_in_tenant(
            session, workspace_id, auth.user_id or "system", role
        ) as tenant:
            stored_id, duplicate = await self._repo.insert(
                tenant,
                run_id=run_id,
                workspace_id=workspace_id,
                external_id=req.external_id,
                agent_name=agent_name,
                task=task,
                task_embedding=embedding,
                outcome=req.outcome,
                outcome_signals=req.outcome_signals.model_dump(exclude_none=True),
                trace=steps,
                trace_digest=digest,
                step_count=len(req.steps),
                tokens_used=req.tokens_used,
                duration_ms=req.duration_ms,
                harness=req.harness,
                ingest_mode="live",
                eligible=eligible,
                ineligible_reason=ineligible_reason,
            )
            await tenant.commit()

        # ── phase 3: hand off ────────────────────────────────────────────────
        # Best-effort by design (``enqueue`` swallows queue errors): a Redis outage
        # must not fail a push whose payload the client has already discarded. The
        # run sits at ``eligible IS NULL`` and the ungated index exists so a
        # backstop can find it.
        if steps is not None:
            await enqueue("gate_run", workspace_id, stored_id)

        return RunAccepted(run_id=stored_id, duplicate=duplicate)
