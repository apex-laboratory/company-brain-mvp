"""Provider-agnostic chat wrappers: JSON-mode calls with retry and cost capture.

Every public call returns ``(parsed_json, StageUsage)``. A malformed JSON reply
gets exactly one reprompt (with the parse error appended) before raising
:class:`LLMParseError` — a permanent error, not retried by ``with_retries``.

The actual SDK call is dispatched through :func:`app.pipeline.llm.providers.get_provider`,
selected at runtime by ``settings.llm_provider`` — callers never import a
provider SDK or know which one is active.
"""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

from app.pipeline.llm.pricing import cost_usd
from app.pipeline.llm.providers import get_provider
from app.pipeline.llm.retry import (
    INTERACTIVE_ATTEMPTS,
    INTERACTIVE_RETRY_AFTER_CAP,
    with_retries,
)
from app.pipeline.types import StageUsage

log = logging.getLogger(__name__)


class LLMParseError(Exception):
    """The model returned non-JSON output twice in a row."""


def _parse_json(text: str) -> dict:
    """Parse a JSON object, tolerating markdown fences and leading prose.

    Reasoning models (common among OpenRouter's free tier) narrate
    chain-of-thought in the same ``content`` field ahead of the actual answer,
    even when told not to — so the whole response often isn't valid JSON on
    its own, just some suffix of it. Scans ``{`` positions from the end (the
    answer is normally last) and accepts the first one whose decoded object
    reaches the true end of the text (only trailing whitespace/fences after
    it) — that's the strongest signal it's the real answer, not JSON quoted
    inside the reasoning. Falls back to the last successfully-parsed object
    if nothing satisfies that.
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned
        cleaned = cleaned.rsplit("```", 1)[0].strip()

    decoder = json.JSONDecoder()
    fallback: dict | None = None
    for start in (i for i, ch in reversed(list(enumerate(cleaned))) if ch == "{"):
        try:
            parsed, end = decoder.raw_decode(cleaned, start)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict):
            continue
        if fallback is None:
            fallback = parsed
        if cleaned[end:].strip(" `\n") == "":
            return parsed
    if fallback is not None:
        return fallback
    raise json.JSONDecodeError("expected a JSON object", cleaned, 0)


async def _json_call(
    system: str,
    user: str,
    *,
    stage: str,
    max_tokens: int,
    attempts: int | None = None,
    max_retry_after: float | None = None,
) -> tuple[dict, StageUsage]:
    """Retry-wrapped provider call + JSON parse with a single reprompt on parse failure."""
    provider = get_provider()
    in_tok = out_tok = 0

    async def _attempt(prompt_user: str) -> tuple[str, int, int]:
        return await with_retries(
            lambda: provider.call(system, prompt_user, max_tokens=max_tokens),
            stage=stage,
            attempts=attempts,
            max_retry_after=max_retry_after,
        )

    text, i, o = await _attempt(user)
    in_tok, out_tok = in_tok + i, out_tok + o
    try:
        parsed = _parse_json(text)
    except json.JSONDecodeError as first_err:
        log.warning("llm %s returned non-JSON; reprompting once", stage)
        reprompt = (
            f"{user}\n\nYour previous reply was not valid JSON "
            f"({first_err.msg}). Respond with a single valid JSON object only."
        )
        text, i, o = await _attempt(reprompt)
        in_tok, out_tok = in_tok + i, out_tok + o
        try:
            parsed = _parse_json(text)
        except json.JSONDecodeError as exc:
            raise LLMParseError(f"{stage}: non-JSON output after reprompt") from exc

    usage = StageUsage(
        stage=stage,
        model=provider.model,
        input_tokens=in_tok,
        output_tokens=out_tok,
        cost_usd=cost_usd(provider.model, in_tok, out_tok),
    )
    return parsed, usage


async def llm_json(
    system: str,
    user: str,
    *,
    stage: str,
    max_tokens: int = 1024,
    interactive: bool = False,
) -> tuple[dict, StageUsage]:
    """JSON-mode chat call, routed to the provider configured by ``LLM_PROVIDER``.

    ``interactive=True`` uses the request-path budget (2 attempts, 5s
    retry-after cap) — the brain synthesizer runs while a user waits."""
    return await _json_call(
        system,
        user,
        stage=stage,
        max_tokens=max_tokens,
        attempts=INTERACTIVE_ATTEMPTS if interactive else None,
        max_retry_after=INTERACTIVE_RETRY_AFTER_CAP if interactive else None,
    )


async def llm_stream(
    system: str, user: str, *, stage: str, max_tokens: int = 2048
) -> AsyncIterator[str | StageUsage]:
    """Stream a reply, yielding text deltas then a final :class:`StageUsage`.

    Deliberately **not** retry-wrapped: a retry mid-stream would replay text the
    caller has already forwarded to the client. A failure here surfaces to the
    caller, which falls back or reports it on the stream.

    Response is requested in JSON mode where the provider supports it, matching
    the non-streaming call, so the brain synthesizer's incremental JSON parser
    sees the same shape — the ``grounded``-before-``answer`` key order the SSE
    path depends on holds regardless of provider.
    """
    provider = get_provider()
    in_tok = out_tok = 0
    async for piece in provider.stream(system, user, max_tokens=max_tokens):
        if isinstance(piece, tuple):
            in_tok, out_tok = piece
        else:
            yield piece
    yield StageUsage(
        stage=stage,
        model=provider.model,
        input_tokens=in_tok,
        output_tokens=out_tok,
        cost_usd=cost_usd(provider.model, in_tok, out_tok),
    )
