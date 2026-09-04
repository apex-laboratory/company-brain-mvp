"""``POST /webhooks/anthropic`` — vendor session-state deliveries (§5.3, phase 4).

Anthropic tells us when a session changes state so we do not have to poll for it.
The mirror in ``agent_sessions`` — status, stop reason, token counts — is kept
current from here, and **only** those; a delivery never carries content and this
module never asks for any.

**Signature verification is the whole of the auth.** There is no JWT and no API
key on a vendor callback: anyone on the internet can POST here, and a forged body
would move a real session's state. So an unverifiable delivery is rejected before
it is parsed, and an unset ``ANTHROPIC_WEBHOOK_SECRET`` rejects *everything* —
fail closed. The alternative, treating a missing secret as "verification off", is
the configuration mistake that silently turns this into an open write endpoint.

**Standard Webhooks, implemented rather than imported.** The scheme is the one
the Anthropic SDK's ``webhooks.unwrap`` uses, but that helper needs the optional
``anthropic[webhooks]`` extra, which is not installed here; taking a new runtime
dependency for twenty-five lines of HMAC would be a worse trade than writing it,
particularly as the repo already verifies GitHub, Slack and Zendesk signatures by
hand in the same shape.

The delivery body is ``{"id", "type": "event", "created_at", "data": {...}}``
where ``data`` carries the session id and the real event type. That is all of it
— no usage, no stop reason, no transcript — so the numbers are read back from the
session afterwards, in the job rather than in this request.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from collections.abc import Mapping
from typing import Any

from app.config.settings import settings
from app.shared.errors.app_error import UnauthorizedError, ValidationError
from app.shared.helpers.crypto import constant_time_compare

log = logging.getLogger(__name__)

# Standard Webhooks header names, lowercase — Starlette's Headers are
# case-insensitive but a plain dict in a test is not.
_ID_HEADER = "webhook-id"
_TIMESTAMP_HEADER = "webhook-timestamp"
_SIGNATURE_HEADER = "webhook-signature"

# Replay window. A delivery older than this is refused even with a valid
# signature: without it, a signature captured once stays a valid write forever.
# Five minutes is the Standard Webhooks default and comfortably exceeds any
# realistic clock skew between us and the vendor.
_TOLERANCE_SECONDS = 300

_SECRET_PREFIX = "whsec_"
_SIGNATURE_VERSION = "v1"

# Session events we act on. Anything else — vault and credential lifecycle,
# thread-level status, outcome evaluations — is acknowledged and dropped: acting
# on an event we have no column for would mean inventing a meaning for it, and
# 400ing on one would make Anthropic retry a delivery that will never succeed.
_HANDLED_PREFIX = "session."


def _header(headers: Mapping[str, str], name: str) -> str | None:
    if name in headers:
        return headers[name]
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return None


def _secret_bytes() -> bytes:
    """Decode the configured ``whsec_``-prefixed signing secret.

    Raises rather than returning empty on a missing secret: the caller must not
    be able to reach a comparison at all without one, because an empty key is a
    perfectly usable HMAC key and would verify a forgery signed with the same
    empty key.
    """
    secret = settings.anthropic_webhook_secret
    if not secret:
        raise UnauthorizedError("Webhook verification is not configured.")
    raw = secret.removeprefix(_SECRET_PREFIX)
    try:
        return base64.b64decode(raw, validate=True)
    except Exception:
        # A secret pasted without base64 encoding. Treat the literal bytes as the
        # key rather than failing outright — Standard Webhooks implementations
        # differ here, and refusing would be a startup failure with no signal.
        return raw.encode()


def verify(headers: Mapping[str, str], raw_body: bytes) -> None:
    """Raise unless this delivery carries a valid, in-window signature.

    Signed content is ``{id}.{timestamp}.{body}`` over the **raw bytes**, which is
    why the router reads the body before anything parses it: re-serializing JSON
    would reorder keys and change the bytes the vendor signed.
    """
    webhook_id = _header(headers, _ID_HEADER)
    timestamp = _header(headers, _TIMESTAMP_HEADER)
    signatures = _header(headers, _SIGNATURE_HEADER)
    if not webhook_id or not timestamp or not signatures:
        raise UnauthorizedError("Webhook signature headers are missing.")

    try:
        sent_at = int(timestamp)
    except ValueError as exc:
        raise UnauthorizedError("Webhook timestamp is malformed.") from exc
    if abs(time.time() - sent_at) > _TOLERANCE_SECONDS:
        raise UnauthorizedError("Webhook timestamp is outside the allowed window.")

    signed = b"%s.%s." % (webhook_id.encode(), timestamp.encode()) + raw_body
    expected = hmac.new(_secret_bytes(), signed, hashlib.sha256).digest()

    # The header is a space-separated list so a secret can be rotated with both
    # keys live. Every candidate is compared in constant time and the loop is not
    # short-circuited on a match, so the work done does not depend on which one
    # matched or on how many were sent.
    matched = False
    for candidate in signatures.split(" "):
        version, _, value = candidate.partition(",")
        if version != _SIGNATURE_VERSION or not value:
            continue
        try:
            decoded = base64.b64decode(value, validate=True)
        except Exception:  # noqa: BLE001 — a malformed candidate is just not a match
            continue
        if constant_time_compare(decoded, expected):
            matched = True
    if not matched:
        raise UnauthorizedError("Webhook signature did not verify.")


def parse(raw_body: bytes) -> tuple[str, str, str] | None:
    """Return ``(delivery_id, event_type, session_id)`` for a session event.

    ``None`` means "verified, understood, and nothing for us to do" — a vault or
    credential lifecycle event, or a session event with no id. The caller answers
    200 either way: a 4xx would make Anthropic retry a delivery that cannot ever
    succeed, and a webhook endpoint that retries forever on events it does not
    handle is one that eventually rate-limits the ones it does.
    """
    try:
        body = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise ValidationError({"body": "Webhook body is not valid JSON."}) from exc
    if not isinstance(body, dict):
        raise ValidationError({"body": "Webhook body is not an object."})

    data = body.get("data")
    if not isinstance(data, dict):
        return None
    event_type = str(data.get("type") or "")
    session_id = str(data.get("id") or "")
    if not event_type.startswith(_HANDLED_PREFIX) or not session_id:
        return None
    return str(body.get("id") or session_id), event_type, session_id


def summarize(payload: Any) -> dict[str, str]:
    """What this endpoint returns. Deliberately says nothing about the session."""
    return {"status": "accepted"} if payload else {"status": "ignored"}
