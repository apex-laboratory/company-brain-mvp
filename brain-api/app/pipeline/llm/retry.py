"""Transient-aware retry wrapper for pipeline LLM calls (PRD Phase 3 failure handling).

Modeled on ``app/integrations/google_common.api_request``: exponential backoff,
``retry-after`` honored when the SDK surfaces it, transient errors only. The
Anthropic/OpenAI SDKs expose ``.status_code``; the Gemini SDK (``google-genai``)
exposes ``.code`` instead — ``_status_code`` checks both:

* transient → retry: ``RateLimitError``/``ClientError`` (429), ``ServerError``/
  ``APIStatusError`` >= 500, connection-level failures
* permanent → raise immediately: 4xx status errors (bad request, auth, ...)

Exhausted retries raise :class:`LLMExhaustedError`; the ARQ task translates that
into ``source_events.outcome = 'failed'`` (dead-letter). ARQ's own ``retry_jobs``
must NOT re-run the task on top of this — the task swallows the error after
finalizing the event.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

from app.config.settings import settings

log = logging.getLogger(__name__)

T = TypeVar("T")  # runtime is 3.11 — PEP 695 syntax not available yet

_MAX_RETRY_AFTER = 400.0  # cap a single honored retry-after sleep (seconds)


class LLMExhaustedError(Exception):
    """A transient LLM failure persisted through every retry attempt."""


def _status_code(exc: Exception) -> int | None:
    """Best-effort status code across the three SDKs — Anthropic/OpenAI expose
    ``.status_code``, the Gemini SDK's ``APIError`` exposes ``.code`` instead."""
    code = getattr(exc, "status_code", None)
    if isinstance(code, int):
        return code
    code = getattr(exc, "code", None)
    return code if isinstance(code, int) else None


def _retry_after(exc: Exception) -> float | None:
    """Honor a retry-after header when the SDK carries the response."""
    response = getattr(exc, "response", None)
    header = getattr(getattr(response, "headers", None), "get", lambda _k: None)(
        "retry-after"
    )
    if header:
        try:
            return max(1.0, min(float(header), _MAX_RETRY_AFTER))
        except ValueError:
            pass
    return None


def is_transient(exc: Exception) -> bool:
    """Transient = worth retrying: 429, 408, 5xx, or a connection-level failure."""
    code = _status_code(exc)
    if code is not None:
        return code == 429 or code == 408 or code >= 500
    # No status code → connection error / timeout from any of the SDKs
    # (APIConnectionError, APITimeoutError all subclass their APIError without
    # a status_code). Plain programming errors (TypeError, KeyError...) are not
    # SDK errors and must not be retried — detect SDK-ness by module origin.
    module = type(exc).__module__ or ""
    return module.startswith(("google.genai", "anthropic", "openai", "httpx", "requests"))


async def with_retries(  # noqa: UP047 — venv runs Python 3.11 (no PEP 695)
    call: Callable[[], Awaitable[T]],
    *,
    stage: str,
    attempts: int | None = None,
) -> T:
    """Run ``call`` with exponential backoff on transient errors.

    ``call`` is a zero-arg coroutine factory so each attempt issues a fresh
    request. Permanent errors propagate untouched; exhausted transient errors
    raise :class:`LLMExhaustedError` chained to the last failure.
    """
    max_attempts = attempts or settings.llm_max_attempts
    last: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return await call()
        except Exception as exc:  # noqa: BLE001 — classified below
            if not is_transient(exc):
                raise
            last = exc
            if attempt + 1 < max_attempts:
                delay = _retry_after(exc) or float(2**attempt)
                log.warning(
                    "llm %s transient failure (%s); retrying in %.1fs (attempt %d/%d)",
                    stage, type(exc).__name__, delay, attempt + 1, max_attempts,
                )
                await asyncio.sleep(delay)
    raise LLMExhaustedError(
        f"{stage}: transient LLM failure after {max_attempts} attempts"
    ) from last
