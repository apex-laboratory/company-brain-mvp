"""Secret redaction + payload digesting for run traces (PRD Feature 29, step 1).

A trace is the most sensitive payload the system accepts: it carries shell output,
file contents, tool arguments and customer records from a machine we do not own.
This module is the gate between that payload and the database.

Three rules, in order of importance:

1. **Fail closed.** If redaction raises, the caller marks the run
   ``ineligible_reason = 'redaction_failed'`` and drops the trace body. We never
   store an unredacted payload "just in case" — an un-scrubbed credential in
   ``agent_runs.trace`` would outlive the incident that leaked it.
2. **Redact by key *and* by value.** Pattern-matching alone misses a secret with
   no recognisable shape (``{"password": "hunter2"}``); key-matching alone misses
   one pasted into free-form shell output. Both run on every step.
3. **Digest anything large.** Payloads over ``max_payload_bytes`` are replaced by
   a digest before storage — the same rule the retention job applies later, so a
   step's shape does not change when the trace is nulled.

Deliberately deterministic: no LLM, no network. This runs on the ingest path where
a `202` is promised in one round-trip, and a probabilistic redactor is not something
to put between a customer's shell output and our disk.

**This is the second line of defence, not the first.** The hook shim redacts on the
customer's machine before anything leaves it (``docs/AGENT_HOOK_SHIM.md``); this pass
assumes that one was skipped, bypassed, or written against an older pattern list.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

REDACTED = "[redacted]"

# Nesting depth beyond which we stop walking and digest the subtree. Guards against
# a hostile or pathological payload turning ingest into a stack overflow.
_MAX_DEPTH = 8

# Keys whose *value* is dropped wholesale, whatever it looks like. Matched against
# the lowercased key with separators stripped, so "API_KEY", "api-key" and
# "apiKey" all collapse to the same token.
_SENSITIVE_KEYS: frozenset[str] = frozenset({
    "apikey", "secret", "secrets", "token", "accesstoken", "refreshtoken",
    "idtoken", "password", "passwd", "pwd", "authorization", "auth",
    "privatekey", "clientsecret", "sessionkey", "cookie", "credentials",
    "connectionstring", "dsn", "encryptionkey", "signingkey", "webhooksecret",
})

# Value patterns, applied to every string. Ordered: structured, high-confidence
# shapes first, so a connection string is masked as a unit before the generic
# assignment rule gets a chance to mangle half of it.
_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # PEM private key blocks (multi-line).
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
    ),
    # URIs carrying inline credentials: postgres://user:pw@host, redis://…, etc.
    re.compile(
        r"\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|rediss|amqp|amqps)"
        r"://[^\s/@]+:[^\s/@]+@\S+",
        re.IGNORECASE,
    ),
    # Authorization headers, in any of the shapes a shell/HTTP log emits.
    re.compile(r"(?i)\bauthorization\b\s*[:=]\s*\S+"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{12,}=*"),
    # Vendor-shaped keys.
    re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{16,}"),          # OpenAI / Anthropic
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),              # GitHub
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                      # AWS access key id
    re.compile(r"\bASIA[0-9A-Z]{16}\b"),                      # AWS session key id
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),            # Slack
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"),                  # Google API key
    re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}"),                # GitLab
    re.compile(r"\bnpm_[A-Za-z0-9]{30,}"),                    # npm
    # JWTs — three base64url segments. Matches our own access tokens too, which is
    # the point: an agent that echoed its own credential must not persist it.
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    # Generic `NAME=value` / `NAME: value` assignments for secret-ish names. Last,
    # so the specific rules above claim their matches first.
    re.compile(
        r"(?i)\b(?:api[_-]?key|secret|access[_-]?token|refresh[_-]?token|auth[_-]?token"
        r"|password|passwd|private[_-]?key|client[_-]?secret)\b\s*[:=]\s*"
        r"[\"']?[^\s\"',;]{6,}"
    ),
)


class RedactionFailed(Exception):
    """Redaction could not complete — the caller must drop the trace body.

    Raised rather than returning a partial result on purpose: a half-redacted
    trace is indistinguishable from a clean one once it is a row in the database.
    """


def _normalize_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def scrub_text(value: str) -> str:
    """Mask every known secret shape in a free-form string."""
    for pattern in _VALUE_PATTERNS:
        value = pattern.sub(REDACTED, value)
    return value


def digest_of(value: Any) -> dict[str, Any]:
    """Replace a payload with a stable, non-reversible summary.

    Keeps what the trajectory compressor actually needs — did this step return a
    lot, was it the same result as last time — while storing none of the content.
    """
    try:
        raw = value if isinstance(value, str) else json.dumps(value, default=str, sort_keys=True)
    except Exception as exc:  # noqa: BLE001 — any serialization failure must fail closed
        raise RedactionFailed(f"payload is not serializable: {exc}") from exc
    encoded = raw.encode("utf-8", errors="replace")
    return {
        "sha256": hashlib.sha256(encoded).hexdigest()[:16],
        "bytes": len(encoded),
        "lines": raw.count("\n") + 1,
    }


def _scrub(value: Any, *, max_payload_bytes: int, depth: int = 0) -> Any:
    """Recursively scrub a JSON-ish value, digesting oversize or too-deep subtrees."""
    if depth > _MAX_DEPTH:
        return digest_of(value)

    if isinstance(value, str):
        scrubbed = scrub_text(value)
        if len(scrubbed.encode("utf-8", errors="replace")) > max_payload_bytes:
            return digest_of(scrubbed)
        return scrubbed

    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            key_str = str(key)
            if _normalize_key(key_str) in _SENSITIVE_KEYS:
                out[key_str] = REDACTED
                continue
            out[key_str] = _scrub(item, max_payload_bytes=max_payload_bytes, depth=depth + 1)
        return out

    if isinstance(value, list):
        return [
            _scrub(item, max_payload_bytes=max_payload_bytes, depth=depth + 1)
            for item in value
        ]

    if value is None or isinstance(value, (int, float, bool)):
        # JSON scalars pass through: no secret survives as a non-string scalar, and
        # coercing them would corrupt the index/latency fields the compressor reads.
        return value

    # Anything else is not JSON and would blow up at the INSERT's json.dumps, after
    # redaction has already "passed". Digest it here so an exotic payload fails
    # closed inside the redactor instead of raising from the repository.
    return digest_of(value)


def redact_step(step: dict[str, Any], *, max_payload_bytes: int) -> dict[str, Any]:
    """Scrub one normalized step envelope in place-safe fashion (returns a copy)."""
    try:
        scrubbed = _scrub(step, max_payload_bytes=max_payload_bytes)
    except RedactionFailed:
        raise
    except Exception as exc:  # noqa: BLE001 — any failure here must fail closed
        raise RedactionFailed(f"step redaction failed: {exc}") from exc
    if not isinstance(scrubbed, dict):  # pragma: no cover — _scrub preserves dicts
        raise RedactionFailed("step did not survive redaction as an object")
    return scrubbed


def redact_steps(
    steps: list[dict[str, Any]], *, max_payload_bytes: int
) -> list[dict[str, Any]]:
    """Scrub a whole trace. Raises ``RedactionFailed`` — never returns partial output."""
    return [redact_step(step, max_payload_bytes=max_payload_bytes) for step in steps]
