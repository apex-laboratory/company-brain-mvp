"""Agent-run ingestion schemas (PRD Feature 29 — the normalized trace envelope).

The envelope is the contract every harness adapter targets: Claude Code hooks
(``docs/AGENT_HOOK_SHIM.md``), the Agent SDK, OpenAI-style tool-call message
lists, LangSmith runs, raw OTel spans. Keeping it narrow is the point — the
extraction engine downstream reads *only* these fields, so a new harness costs an
adapter and nothing else.

Request schemas reject unknown keys (``CamelRequestModel``): a harness sending a
field we don't model is a bug in the adapter, and silently dropping it would hide
data loss until someone wondered why a procedure was missing a step.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator

from app.config.settings import settings
from app.shared.schemas import CamelModel, CamelRequestModel

# ``file_write`` is a Brainite addition to the PRD's original four. Folding writes
# into ``tool_call`` would erase the read/write distinction, which is precisely the
# signal a procedure needs: "read the policy, then edit the config" and "read the
# policy, then read the config" are different procedures.
StepType = Literal["tool_call", "file_read", "file_write", "shell", "assistant_message"]
StepStatus = Literal["ok", "error", "skipped"]

# Caller-reported. ``ambiguous`` is a first-class value, not a gap: a harness that
# cannot prove success must be able to say so. The gate excludes it (Feature 30)
# rather than the client guessing, so the judgement lives in one place.
Outcome = Literal["success", "failure", "partial", "ambiguous", "unknown"]

Harness = Literal[
    "claude_code", "agent_sdk", "openai", "langsmith", "langfuse", "otel", "custom"
]


class RunStep(CamelRequestModel):
    """One step of a trace. ``args``/``text`` are redacted at ingest, never as-is."""

    index: Annotated[int, Field(ge=0)]
    type: StepType
    name: Annotated[str | None, Field(default=None, max_length=2000)]
    args: dict[str, Any] | None = None
    text: Annotated[str | None, Field(default=None, max_length=100_000)]
    result_digest: Annotated[str | None, Field(default=None, max_length=200)]
    status: StepStatus = "ok"
    latency_ms: Annotated[int | None, Field(default=None, ge=0)] = None


class OutcomeSignals(CamelRequestModel):
    """Evidence behind the reported outcome. Every field is optional and tri-state:
    ``True``/``False`` are assertions, ``None`` means "this harness cannot tell".

    ``human_confirmed`` is the strongest signal in the system — it bypasses the
    ``min_runs_per_cluster`` wait (Feature 30) — so it must only be set when a
    person actually said so (the shim's ``/brain-done``), never inferred.
    """

    human_confirmed: bool | None = None
    tests_passed: bool | None = None
    ticket_resolved: bool | None = None
    no_override: bool | None = None
    error_free_tail: bool | None = None
    # Set by adapters that derived the outcome rather than reading it from the
    # harness. Caps the resulting skill's authority (Feature 29a) — nobody
    # confirmed the run, so the procedure it yields cannot claim they did.
    inferred: bool | None = None


class RunIngestRequest(CamelRequestModel):
    """``POST /runs`` body — one task attempt by one agent."""

    external_id: Annotated[str | None, Field(default=None, max_length=255)]
    agent_name: Annotated[str, Field(min_length=1, max_length=255)]
    task: Annotated[str, Field(min_length=1, max_length=8000)]
    outcome: Outcome
    outcome_signals: OutcomeSignals = OutcomeSignals()
    harness: Harness = "custom"
    started_at: datetime | None = None
    ended_at: datetime | None = None
    tokens_used: Annotated[int | None, Field(default=None, ge=0)] = None
    duration_ms: Annotated[int | None, Field(default=None, ge=0)] = None
    # The step cap is enforced here as well as by the body-size limit: a caller
    # that blows the cap gets a 422 naming the field, not an opaque 413.
    steps: Annotated[list[RunStep], Field(min_length=1, max_length=settings.run_max_steps)]

    @field_validator("steps", mode="after")
    @classmethod
    def _steps_ordered(cls, steps: list[RunStep]) -> list[RunStep]:
        """Reject out-of-order or duplicated indices.

        Trajectory compression (Feature 31) reads the step list as a *causal*
        sequence — "the agent read the policy, then ran the script". A shuffled
        list would distil into a procedure whose steps are in the wrong order,
        which is worse than no procedure at all.
        """
        expected = list(range(len(steps)))
        actual = [s.index for s in steps]
        if actual != expected:
            raise ValueError(
                "steps must be indexed contiguously from 0 in execution order; "
                f"got {actual[:8]}{'…' if len(actual) > 8 else ''}"
            )
        return steps


class RunAccepted(CamelModel):
    """``202`` body. Gating and distillation are async — this is a receipt, not a verdict.

    ``duplicate`` tells an idempotent re-push apart from a first ingest so a
    retrying harness can stop retrying; both are ``202``, because both leave the
    server in the state the caller wanted.
    """

    run_id: str
    duplicate: bool = False
