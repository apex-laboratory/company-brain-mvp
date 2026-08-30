"""The Brainite plugin's envelope, validated against this schema.

The producer (``plugin/``, Go) and the consumer (``RunIngestRequest``, pydantic)
live in different languages, so nothing but a shared fixture stops them drifting:
the client can keep emitting a field this schema silently rejects — request models
are ``CamelRequestModel`` and forbid unknown keys — and no test on either side
would notice until a developer's traces stopped arriving.

The fixtures are produced *by the real client* from *real captured hook payloads*
(``plugin/testdata/golden``), not written by hand. Regenerate with:

    cd plugin && BRAINITE_WRITE_FIXTURE=1 go test ./internal/handlers -run TestWriteEnvelopeFixture

A failure here means the client and the server disagree about the wire format.
Fix whichever side is wrong; do not edit the fixture by hand.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config.settings import settings
from app.modules.runs.schemas import RunIngestRequest
from app.modules.runs.service import build_digest

FIXTURE_DIR = Path(__file__).resolve().parents[4] / "plugin" / "testdata" / "envelope"
FIXTURES = ["inferred_run.json", "human_confirmed_run.json", "failed_step_run.json"]

pytestmark = pytest.mark.skipif(
    not FIXTURE_DIR.is_dir(), reason="plugin fixtures not present in this checkout"
)


def _load(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text())


@pytest.mark.parametrize("name", FIXTURES)
def test_plugin_envelope_validates(name: str) -> None:
    """Every envelope the client emits is accepted by the ingest schema."""
    try:
        req = RunIngestRequest.model_validate(_load(name))
    except ValidationError as exc:  # pragma: no cover - the message is the point
        pytest.fail(f"{name} is rejected by RunIngestRequest:\n{exc}")

    assert req.harness == "claude_code"
    assert req.agent_name
    assert req.task
    assert req.steps


@pytest.mark.parametrize("name", FIXTURES)
def test_plugin_envelope_carries_no_raw_payloads(name: str) -> None:
    """Only digests cross the wire.

    The client must never ship ``tool_response`` content — that is file contents
    and shell output from a machine we do not own. ``result_digest`` is the whole
    point of the step shape, and a client regression that started sending ``text``
    on tool steps would be a privacy incident this catches before it ships.
    """
    req = RunIngestRequest.model_validate(_load(name))
    for step in req.steps:
        if step.type == "assistant_message":
            continue  # the final message is deliberately carried in full
        assert step.text is None, f"{name}: step {step.index} carries raw text"
        if step.result_digest:
            assert "bytes=" in step.result_digest and "lines=" in step.result_digest


def test_failed_tool_call_reaches_the_server_as_an_error_step() -> None:
    """A failed tool call must arrive as ``status='error'``.

    Claude Code emits **no PostToolUse for a failed tool call**, so the client
    derives this from a step that opened and never closed. If that ever regresses,
    every run in the corpus looks clean, the gate's error-free-tail check is
    defeated, and failures get distilled into procedures.
    """
    req = RunIngestRequest.model_validate(_load("failed_step_run.json"))
    assert any(s.status == "error" for s in req.steps), (
        "no error step survived the round trip — failures are invisible to the gate"
    )
    assert req.outcome != "success"


def test_human_confirmed_is_never_marked_inferred() -> None:
    """``human_confirmed`` bypasses ``run_min_runs_per_cluster`` entirely.

    It is the strongest signal in the system, so the client must only set it when
    a person actually said so — never alongside a claim that it was derived.
    """
    req = RunIngestRequest.model_validate(_load("human_confirmed_run.json"))
    assert req.outcome == "success"
    assert req.outcome_signals.human_confirmed is True
    assert req.outcome_signals.inferred is not True


@pytest.mark.parametrize("name", FIXTURES)
def test_plugin_envelope_is_digestible(name: str) -> None:
    """The envelope survives the ingest path's summarisation.

    ``build_digest`` is what outlives the raw trace once retention nulls it, so a
    client whose steps produced an empty digest would leave nothing behind.
    """
    req = RunIngestRequest.model_validate(_load(name))
    digest = build_digest([s.model_dump(exclude_none=True) for s in req.steps], req)
    assert digest["step_count"] == len(req.steps)
    assert digest["harness"] == "claude_code"
    assert digest["sha256"]


@pytest.mark.parametrize("name", FIXTURES)
def test_plugin_envelope_respects_the_server_caps(name: str) -> None:
    """The client caps client-side so a real push never eats a 413 or a 422."""
    body = _load(name)
    assert len(body["steps"]) <= settings.run_max_steps
    assert len(json.dumps(body).encode()) <= settings.run_max_body_bytes
