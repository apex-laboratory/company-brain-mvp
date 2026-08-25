"""Success gate tests (PRD Feature 30).

``evaluate`` is the single most consequential predicate in the self-improving
loop: everything it lets through eventually costs a Sonnet call and a reviewer's
attention, and everything it wrongly rejects is a procedure the brain never
learns. It is pure, so it is asserted as a table.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config.settings import settings
from app.jobs.tasks import gate_run as gate_module
from app.jobs.tasks.gate_run import evaluate, gate_run


def _run(**over: Any) -> dict[str, Any]:
    base = {
        "id": "run_1",
        "agent_name": "support-triage-agent",
        "task": "Refund a damaged order past the 30-day window",
        "task_embedding": [0.0] * 1536,
        "outcome": "success",
        "outcome_signals": {"human_confirmed": False},
        "trace": [
            {"index": 0, "type": "tool_call", "name": "query_brain", "status": "ok"},
            {"index": 1, "type": "file_read", "name": "policies/refunds.md", "status": "ok"},
            {"index": 2, "type": "shell", "name": "python check_order.py", "status": "ok"},
            {"index": 3, "type": "tool_call", "name": "approve_refund", "status": "ok"},
        ],
        "trace_digest": {"sha256": "abc123"},
        "step_count": 4,
        "eligible": None,
        "distilled_at": None,
    }
    return {**base, **over}


def test_clean_success_is_eligible() -> None:
    assert evaluate(_run()) == (True, None)


@pytest.mark.parametrize("outcome", ["failure", "partial", "unknown", "ambiguous"])
def test_non_success_outcomes_are_rejected(outcome: str) -> None:
    """``ambiguous`` in particular: the client is expected to report it honestly
    rather than guess, which only works if the gate excludes it here."""
    assert evaluate(_run(outcome=outcome)) == (False, "not_successful")


def test_human_override_excludes_the_run() -> None:
    """A person corrected the agent mid-run — the trace records what the agent
    tried, not what the company does."""
    eligible, reason = evaluate(_run(outcome_signals={"no_override": False}))
    assert (eligible, reason) == (False, "policy_excluded")


def test_too_few_steps_is_trivial() -> None:
    eligible, reason = evaluate(_run(step_count=1, trace=[]))
    assert (eligible, reason) == (False, "too_trivial")


def test_error_in_the_tail_overrides_a_success_label() -> None:
    """The label says success; the evidence disagrees. Trust the evidence."""
    trace = _run()["trace"]
    trace[-1]["status"] = "error"
    eligible, reason = evaluate(_run(trace=trace))
    assert (eligible, reason) == (False, "not_successful")


def test_error_early_in_the_run_is_fine() -> None:
    """Dead ends are normal and are exactly what trajectory compression removes —
    rejecting them would throw away most real runs."""
    trace = [
        {"index": 0, "type": "shell", "name": "python check_order.py", "status": "error"},
        {"index": 1, "type": "shell", "name": "python check_order.py --prod", "status": "ok"},
        {"index": 2, "type": "tool_call", "name": "approve_refund", "status": "ok"},
        {"index": 3, "type": "assistant_message", "status": "ok"},
    ]
    assert evaluate(_run(trace=trace)) == (True, None)


# ── job-level behaviour ──────────────────────────────────────────────────────
class _AsyncCtx:
    def __init__(self, value: object) -> None:
        self._value = value

    async def __aenter__(self) -> object:
        return self._value

    async def __aexit__(self, *_: object) -> bool:
        return False


def _patched(repo: MagicMock):
    session = MagicMock(commit=AsyncMock())
    return (
        patch.object(gate_module, "get_tenant_session", return_value=_AsyncCtx(session)),
        patch.object(gate_module, "run_in_tenant", return_value=_AsyncCtx(session)),
        patch.object(gate_module, "_repo", repo),
    )


async def _run_job(repo: MagicMock, workspace: str = "wrk_1") -> dict:
    patches = _patched(repo)
    for p in patches:
        p.start()
    try:
        return await gate_run({}, workspace, "run_1")
    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_cross_tenant_run_is_invisible_to_the_gate() -> None:
    """RLS scopes the lookup: a run id from another workspace simply isn't there.
    The job must treat that as a no-op, never as an error to retry forever."""
    repo = MagicMock(get_for_gate=AsyncMock(return_value=None))
    result = await _run_job(repo, workspace="wrk_other")
    assert result["outcome"] == "not_found"
    repo.mark_gated.assert_not_called()


@pytest.mark.asyncio
async def test_duplicate_trace_is_rejected_after_the_cheap_checks() -> None:
    """Identical traces are one piece of evidence, not N — counting them N times
    is how min_runs_per_cluster gets gamed by accident."""
    repo = MagicMock(
        get_for_gate=AsyncMock(return_value=_run()),
        duplicate_digest_exists=AsyncMock(return_value=True),
        mark_gated=AsyncMock(),
        nearest_cluster=AsyncMock(),
    )
    result = await _run_job(repo)
    assert result["reason"] == "duplicate_of_run"
    repo.nearest_cluster.assert_not_called(), "must not cluster a rejected run"
    assert repo.mark_gated.await_args.kwargs["cluster_id"] is None


@pytest.mark.asyncio
async def test_first_run_of_a_task_opens_a_cluster_but_does_not_distil() -> None:
    """One success is an anecdote. Distillation waits for corroboration."""
    repo = MagicMock(
        get_for_gate=AsyncMock(return_value=_run()),
        duplicate_digest_exists=AsyncMock(return_value=False),
        nearest_cluster=AsyncMock(return_value=(None, 0)),
        mark_gated=AsyncMock(),
    )
    result = await _run_job(repo)
    assert result["outcome"] == "eligible"
    assert result["cluster_id"].startswith("clu_")
    assert result["ready_for_distillation"] is False


@pytest.mark.asyncio
async def test_cluster_reaching_the_threshold_is_ready() -> None:
    repo = MagicMock(
        get_for_gate=AsyncMock(return_value=_run()),
        duplicate_digest_exists=AsyncMock(return_value=False),
        nearest_cluster=AsyncMock(
            return_value=("clu_existing", settings.run_min_runs_per_cluster - 1)
        ),
        mark_gated=AsyncMock(),
    )
    result = await _run_job(repo)
    assert result["cluster_id"] == "clu_existing"
    assert result["ready_for_distillation"] is True


@pytest.mark.asyncio
async def test_human_confirmed_run_bypasses_the_threshold() -> None:
    """``/brain-done`` is the strongest signal in the system: a person supplied the
    corroboration the threshold is only a proxy for."""
    repo = MagicMock(
        get_for_gate=AsyncMock(
            return_value=_run(outcome_signals={"human_confirmed": True})
        ),
        duplicate_digest_exists=AsyncMock(return_value=False),
        nearest_cluster=AsyncMock(return_value=(None, 0)),
        mark_gated=AsyncMock(),
    )
    result = await _run_job(repo)
    assert result["ready_for_distillation"] is True, "must not wait for 3 runs"


@pytest.mark.asyncio
async def test_eligible_but_unembedded_run_is_left_unclustered() -> None:
    """An embedding outage must not silently drop the run — it stays eligible and
    unclustered so a re-embed can pick it up."""
    repo = MagicMock(
        get_for_gate=AsyncMock(return_value=_run(task_embedding=None)),
        duplicate_digest_exists=AsyncMock(return_value=False),
        nearest_cluster=AsyncMock(),
        mark_gated=AsyncMock(),
    )
    result = await _run_job(repo)
    assert result["outcome"] == "eligible"
    assert result["cluster_id"] is None
    repo.nearest_cluster.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# pgvector round-trip
# ─────────────────────────────────────────────────────────────────────────────


def test_parse_vector_handles_the_drivers_text_literal() -> None:
    """A ``vector`` column comes back as text, not as a sequence.

    No pgvector codec is registered on the asyncpg connection, so the column
    arrives as ``'[0.1,0.2]'``. Before this was parsed at the repository
    boundary, the gate ran ``list()`` over that string and produced a list of
    *characters*, and clustering died on ``float('[')`` — every run reached the
    gate and none was ever clustered. Unit tests could not catch it because they
    inject a mocked repository; only a live round-trip did.
    """
    from app.modules.runs.repository import _parse_vector

    assert _parse_vector("[0.1,0.2,-0.3]") == [0.1, 0.2, -0.3]
    assert _parse_vector("[]") == []
    assert _parse_vector(None) is None
    # Already-parsed input (a codec registered later, or a test double) must
    # survive unchanged rather than being re-parsed.
    assert _parse_vector([0.1, 0.2]) == [0.1, 0.2]


def test_parse_vector_output_is_usable_by_the_gate() -> None:
    """The parsed value must survive the ``list(...)`` the gate applies to it."""
    from app.modules.runs.repository import _parse_vector, _vector_literal

    original = [0.125, -0.5, 0.75]
    parsed = _parse_vector(_vector_literal(original))
    assert parsed == original
    assert [float(v) for v in list(parsed)] == original
