"""Redaction tests (PRD Feature 29 step 1).

The trace is the most sensitive payload the system accepts, so these are asserted
as a table of concrete leak shapes rather than a couple of happy paths: every row
here is a credential that would otherwise be sitting in ``agent_runs.trace``.
"""
from __future__ import annotations

import pytest

from app.modules.runs.redaction import (
    REDACTED,
    RedactionFailed,
    digest_of,
    redact_step,
    redact_steps,
    scrub_text,
)

_SECRETS = [
    ("openai", "key is sk-proj-abcdefghijklmnopqrstuvwxyz012345"),
    ("anthropic", "ANTHROPIC_API_KEY=sk-ant-api03-AAAABBBBCCCCDDDDEEEEFFFF"),
    ("github pat", "token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"),
    ("github fine-grained", "github_pat_11ABCDEFG0abcdefghijklmnop"),
    ("aws", "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"),
    ("slack", "xoxb-123456789012-abcdefghijklmnop"),
    ("google", "AIzaSyD-1234567890abcdefghijklmnopqrstuv"),
    ("gitlab", "glpat-ABCDEFGHIJKLMNOPQRST"),
    ("jwt", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.abcdefghijklmnop"),
    ("bearer", "Authorization: Bearer abcdefghijklmnopqrstuvwxyz"),
    ("postgres dsn", "postgresql://admin:hunter2@db.internal:5432/brain"),
    ("mongo srv", "mongodb+srv://root:s3cret@cluster0.example.net/db"),
    ("generic assignment", 'password: "correct-horse-battery"'),
]


@pytest.mark.parametrize("label,text", _SECRETS, ids=[s[0] for s in _SECRETS])
def test_scrub_text_masks_known_secret_shapes(label: str, text: str) -> None:
    scrubbed = scrub_text(text)
    assert REDACTED in scrubbed, f"{label}: nothing was redacted"
    # The secret material itself must be gone, not merely annotated.
    for token in ("hunter2", "s3cret", "correct-horse-battery", "EXAMPLE"):
        assert token not in scrubbed or token not in text


def test_scrub_text_preserves_ordinary_output() -> None:
    """Redaction must not eat the trace. An over-eager scrubber that masks normal
    shell output destroys the trajectory the procedure is distilled from."""
    text = "Ran 42 tests in 3.10s\nOK\nmodified: app/pipeline/orchestrator.py"
    assert scrub_text(text) == text


def test_pem_block_is_masked_whole() -> None:
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEAx\nSECRETLINE\n"
        "-----END RSA PRIVATE KEY-----"
    )
    scrubbed = scrub_text(f"cat id_rsa\n{pem}\ndone")
    assert "SECRETLINE" not in scrubbed
    assert "done" in scrubbed


def test_sensitive_keys_are_dropped_by_name() -> None:
    """A secret with no recognisable shape is caught by its key, not its value."""
    step = {
        "index": 0, "type": "tool_call", "name": "call_api",
        "args": {"apiKey": "plainlookingvalue", "user": "alice", "PASSWORD": "hunter2"},
    }
    out = redact_step(step, max_payload_bytes=2048)
    assert out["args"]["apiKey"] == REDACTED
    assert out["args"]["PASSWORD"] == REDACTED
    assert out["args"]["user"] == "alice", "non-sensitive fields must survive"


def test_oversize_payload_becomes_a_digest() -> None:
    step = {"index": 0, "type": "shell", "name": "cat big.log", "text": "x" * 5000}
    out = redact_step(step, max_payload_bytes=2048)
    assert isinstance(out["text"], dict)
    assert out["text"]["bytes"] == 5000
    assert "x" * 100 not in str(out["text"])


def test_deeply_nested_payload_is_digested_not_recursed() -> None:
    """Guards ingest against a pathological payload turning into a stack overflow."""
    nested: dict = {"leaf": "sk-abcdefghijklmnopqrstuvwx"}
    for _ in range(20):
        nested = {"next": nested}
    out = redact_step(
        {"index": 0, "type": "tool_call", "args": nested}, max_payload_bytes=2048
    )
    assert "sk-abcdefghijklmnopqrstuvwx" not in str(out)


def test_scalars_pass_through_unchanged() -> None:
    """Coercing numbers would corrupt index/latency fields the compressor reads."""
    step = {"index": 3, "type": "shell", "latencyMs": 240, "status": "ok", "ok": True}
    out = redact_step(step, max_payload_bytes=2048)
    assert out["index"] == 3 and out["latencyMs"] == 240 and out["ok"] is True


def test_redact_steps_is_all_or_nothing() -> None:
    """A half-redacted trace is indistinguishable from a clean one once stored, so
    failure must raise rather than return partial output."""

    class Unserializable:
        def __repr__(self) -> str:
            raise RuntimeError("boom")

    with pytest.raises(RedactionFailed):
        redact_steps(
            [{"index": 0, "type": "shell", "args": {"x": Unserializable()}}] * 3,
            max_payload_bytes=1,
        )


def test_digest_is_stable_and_non_reversible() -> None:
    a = digest_of([{"index": 0, "type": "shell", "name": "pytest -q"}])
    b = digest_of([{"index": 0, "type": "shell", "name": "pytest -q"}])
    c = digest_of([{"index": 0, "type": "shell", "name": "pytest -x"}])
    assert a == b, "same trace must digest identically (dedupe depends on it)"
    assert a["sha256"] != c["sha256"]
    assert "pytest" not in str(a)
