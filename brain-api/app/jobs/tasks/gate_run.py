"""``gate_run`` job — the success gate + task clustering (PRD Feature 30).

This is the filter that separates Brainite from every episodic memory layer in the
category. Competitors capture agent sessions and serve them back; we throw most of
them away. A trace only becomes evidence for a procedure if it *demonstrably*
succeeded, and only becomes a distillation once enough independent runs converge
on the same task.

Deliberately **no LLM**. Every check is a cheap deterministic predicate, because
the gate runs on every ingested run and the whole point is to be the cheap layer
in front of the expensive one:

    eligible = outcome == success
      AND no human override on the run
      AND the last 3 steps contain no failed step
      AND min_steps <= step_count <= max_steps
      AND this trace is not a duplicate of an already-ingested one

Ineligible runs are **kept**, with ``eligible = false`` and an
``ineligible_reason``. They are a product signal in their own right (PRD §15
tracks failure rates per agent), and deleting them would mean re-ingesting the
same rejected trace on every retry.

Clustering, not per-run distillation, is the cost control: an agent that runs one
task 500 times yields one Sonnet call, not 500. A single success is an anecdote —
it may have succeeded through luck, a warm cache, or a path that only works for
one customer. ``min_runs_per_cluster`` independent runs converging on the same
spine is evidence. A ``humanConfirmed`` run skips the wait, because a person
already supplied the corroboration the threshold is a proxy for.
"""
from __future__ import annotations

import logging
from typing import Any

from app.config.database import get_tenant_session
from app.config.settings import settings
from app.modules.runs.repository import RunsRepository
from app.shared.helpers.ids import generate_id
from app.shared.middleware.with_tenant import run_in_tenant

log = logging.getLogger(__name__)

# How many trailing steps must be clean for a "success" to be believed. A run
# whose final actions errored did not finish the job, whatever its label says —
# and the tail is where a procedure's payoff step lives.
_TAIL_STEPS = 3

_repo = RunsRepository()


def evaluate(run: dict[str, Any]) -> tuple[bool, str | None]:
    """Pure gate predicate: ``(eligible, ineligible_reason)``.

    Split out from the job so it is unit-testable without a database — this is the
    single most consequential piece of logic in the loop, and it must be provable
    against a table of cases rather than exercised through a worker.
    """
    if run.get("outcome") != "success":
        # Covers failure/partial/unknown *and* ambiguous. The client is expected to
        # report ambiguous rather than guess (see the shim's outcome resolver);
        # excluding it here is what makes that honesty free for the client.
        return False, "not_successful"

    signals = run.get("outcome_signals") or {}
    if signals.get("no_override") is False:
        # A human corrected the agent mid-run. The trace records what the agent
        # tried, not what the company does — distilling it would teach the brain
        # the behaviour a person just rejected.
        return False, "policy_excluded"

    step_count = run.get("step_count") or 0
    if step_count < settings.run_min_steps:
        return False, "too_trivial"
    if step_count > settings.run_max_steps:
        return False, "too_noisy"

    trace = run.get("trace") or []
    tail = trace[-_TAIL_STEPS:] if trace else []
    if any(step.get("status") == "error" for step in tail):
        # The label says success; the evidence disagrees. Trust the evidence.
        return False, "not_successful"

    return True, None


async def gate_run(ctx: dict, workspace_id: str, run_id: str) -> dict[str, Any]:
    """ARQ entrypoint. Judges one run and assigns it to a task cluster if eligible."""
    async with get_tenant_session() as session, run_in_tenant(
        session, workspace_id, "system", "admin"
    ) as tenant:
        run = await _repo.get_for_gate(tenant, run_id)
        if run is None:
            # RLS-scoped miss: the run belongs to another workspace, or the
            # workspace was deleted between enqueue and execution. Not an error.
            log.info("gate_run: run=%s not visible in workspace=%s", run_id, workspace_id)
            return {"outcome": "not_found", "run_id": run_id}

        if run.get("distilled_at") is not None:
            return {"outcome": "already_distilled", "run_id": run_id}

        eligible, reason = evaluate(run)

        # The duplicate check needs a query, so it runs after the cheap predicates
        # rather than inside ``evaluate`` — no point paying for it on a run that
        # already failed on step count.
        if eligible:
            sha = (run.get("trace_digest") or {}).get("sha256")
            if sha and await _repo.duplicate_digest_exists(
                tenant, run_id=run_id, agent_name=run["agent_name"], sha=sha
            ):
                eligible, reason = False, "duplicate_of_run"

        cluster_id: str | None = None
        cluster_size = 0
        if eligible:
            embedding = run.get("task_embedding")
            if embedding is None:
                # Ingest stored the run without a vector (embedding outage). It is
                # eligible but unclusterable; leaving cluster_id NULL keeps it
                # visible to a future re-embed instead of silently dropping it.
                log.info("gate_run: run=%s eligible but unembedded; left unclustered", run_id)
            else:
                cluster_id, cluster_size = await _repo.nearest_cluster(
                    tenant,
                    run_id=run_id,
                    agent_name=run["agent_name"],
                    embedding=list(embedding),
                    threshold=settings.run_cluster_threshold,
                )
                if cluster_id is None:
                    cluster_id, cluster_size = generate_id("run_cluster"), 0

        await _repo.mark_gated(
            tenant,
            run_id=run_id,
            eligible=eligible,
            ineligible_reason=reason,
            cluster_id=cluster_id,
        )
        await tenant.commit()

    if not eligible:
        log.info("gate_run: run=%s ineligible (%s)", run_id, reason)
        return {"outcome": "ineligible", "run_id": run_id, "reason": reason}

    members = cluster_size + 1
    human_confirmed = bool((run.get("outcome_signals") or {}).get("human_confirmed"))
    ready = human_confirmed or members >= settings.run_min_runs_per_cluster

    if ready:
        # ── Feature 31/32 seam ────────────────────────────────────────────────
        # Trajectory compression and the procedure extractor are not built yet.
        # This is where ``enqueue("distill_cluster", workspace_id, cluster_id)``
        # goes; until then the cluster stays marked ready and undistilled, so
        # nothing is lost — the distiller drains it when it lands.
        log.info(
            "gate_run: cluster=%s ready for distillation (members=%d, human_confirmed=%s)",
            cluster_id, members, human_confirmed,
        )

    return {
        "outcome": "eligible",
        "run_id": run_id,
        "cluster_id": cluster_id,
        "cluster_size": members,
        "ready_for_distillation": ready,
    }
