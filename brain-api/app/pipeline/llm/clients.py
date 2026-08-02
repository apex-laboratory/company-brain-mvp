"""Gemini + Anthropic chat wrappers: JSON-mode calls with retry and cost capture.

One lazy SDK singleton per provider (mirrors ``http_client()`` in
``app/integrations/base.py``). Keys are validated at first use, not import, so
the API/worker boot without pipeline keys and only extraction fails when they
are missing.

Every public call returns ``(parsed_json, StageUsage)``. A malformed JSON reply
gets exactly one reprompt (with the parse error appended) before raising
:class:`LLMParseError` — a permanent error, not retried by ``with_retries``.
"""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from app.config.settings import settings
from app.pipeline.llm.pricing import cost_usd
from app.pipeline.llm.retry import (
    INTERACTIVE_ATTEMPTS,
    INTERACTIVE_RETRY_AFTER_CAP,
    with_retries,
)
from app.pipeline.types import StageUsage

log = logging.getLogger(__name__)

_gemini_client: Any = None
_anthropic_client: Any = None


class LLMParseError(Exception):
    """The model returned non-JSON output twice in a row."""


def _require_key(name: str, value: str) -> str:
    if not value:
        raise RuntimeError(
            f"{name} is not configured — the extraction pipeline needs it. "
            f"Set it in brain-api/.env."
        )
    return value


def gemini_client() -> Any:
    """Lazy ``genai.Client`` singleton (used via its ``.aio`` async surface)."""
    global _gemini_client
    if _gemini_client is None:
        from google import genai

        _gemini_client = genai.Client(
            api_key=_require_key("GEMINI_API_KEY", settings.gemini_api_key)
        )
    return _gemini_client


def anthropic_client() -> Any:
    """Lazy ``AsyncAnthropic`` singleton."""
    global _anthropic_client
    if _anthropic_client is None:
        import httpx
        from anthropic import AsyncAnthropic

        # SDK default is 600s + 2 internal retries; synthesis (2048 max_tokens)
        # finishes well inside 120s. Fail fast — with_retries owns retrying.
        _anthropic_client = AsyncAnthropic(
            api_key=_require_key("ANTHROPIC_API_KEY", settings.anthropic_api_key),
            timeout=httpx.Timeout(120.0, connect=5.0),
            max_retries=1,
        )
    return _anthropic_client


def _parse_json(text: str) -> dict:
    """Parse a JSON object, tolerating markdown code fences around it."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned
        cleaned = cleaned.rsplit("```", 1)[0].strip()
    parsed = json.loads(cleaned)
    if not isinstance(parsed, dict):
        raise json.JSONDecodeError("expected a JSON object", cleaned, 0)
    return parsed


async def _gemini_call(system: str, user: str, *, max_tokens: int) -> tuple[str, int, int]:
    from google.genai import types

    resp = await gemini_client().aio.models.generate_content(
        model=settings.gemini_model,
        contents=user,
        config=types.GenerateContentConfig(
            system_instruction=system,
            temperature=0.0,
            max_output_tokens=max_tokens,
            response_mime_type="application/json",
        ),
    )
    usage = resp.usage_metadata
    return (
        resp.text or "",
        usage.prompt_token_count if usage and usage.prompt_token_count else 0,
        usage.candidates_token_count if usage and usage.candidates_token_count else 0,
    )


async def _sonnet_call(system: str, user: str, *, max_tokens: int) -> tuple[str, int, int]:
    resp = await anthropic_client().messages.create(
        model=settings.anthropic_model,
        system=system,
        messages=[{"role": "user", "content": user}],
        temperature=0.0,
        max_tokens=max_tokens,
    )
    text = "".join(block.text for block in resp.content if getattr(block, "text", None))
    return text, resp.usage.input_tokens, resp.usage.output_tokens


async def _json_call(
    call,
    system: str,
    user: str,
    *,
    stage: str,
    model: str,
    max_tokens: int,
    attempts: int | None = None,
    max_retry_after: float | None = None,
) -> tuple[dict, StageUsage]:
    """Retry-wrapped call + JSON parse with a single reprompt on parse failure."""
    in_tok = out_tok = 0

    async def _attempt(prompt_user: str) -> tuple[str, int, int]:
        return await with_retries(
            lambda: call(system, prompt_user, max_tokens=max_tokens),
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
        model=model,
        input_tokens=in_tok,
        output_tokens=out_tok,
        cost_usd=cost_usd(model, in_tok, out_tok),
    )
    return parsed, usage


async def gemini_json(
    system: str, user: str, *, stage: str, max_tokens: int = 1024
) -> tuple[dict, StageUsage]:
    """Gemini JSON-mode chat call for the fast classifier + extraction stages."""
    return await _json_call(
        _gemini_call, system, user, stage=stage, model=settings.gemini_model,
        max_tokens=max_tokens,
    )


async def sonnet_json(
    system: str,
    user: str,
    *,
    stage: str,
    max_tokens: int = 2048,
    interactive: bool = False,
) -> tuple[dict, StageUsage]:
    """Sonnet chat call (JSON instructed via prompt).

    ``interactive=True`` uses the request-path budget (2 attempts, 5s
    retry-after cap) — the brain synthesizer runs while a user waits."""
    return await _json_call(
        _sonnet_call,
        system,
        user,
        stage=stage,
        model=settings.anthropic_model,
        max_tokens=max_tokens,
        attempts=INTERACTIVE_ATTEMPTS if interactive else None,
        max_retry_after=INTERACTIVE_RETRY_AFTER_CAP if interactive else None,
    )


async def sonnet_stream(
    system: str, user: str, *, stage: str, max_tokens: int = 2048
) -> AsyncIterator[str | StageUsage]:
    """Stream a Sonnet reply, yielding text deltas then a final :class:`StageUsage`.

    Deliberately **not** retry-wrapped: a retry mid-stream would replay text the
    caller has already forwarded to the client. A failure here surfaces to the
    caller, which falls back or reports it on the stream.
    """
    client = anthropic_client()
    async with client.messages.stream(
        model=settings.anthropic_model,
        system=system,
        messages=[{"role": "user", "content": user}],
        temperature=0.0,
        max_tokens=max_tokens,
    ) as stream:
        async for text in stream.text_stream:
            yield text
        final = await stream.get_final_message()
    in_tok, out_tok = final.usage.input_tokens, final.usage.output_tokens
    yield StageUsage(
        stage=stage,
        model=settings.anthropic_model,
        input_tokens=in_tok,
        output_tokens=out_tok,
        cost_usd=cost_usd(settings.anthropic_model, in_tok, out_tok),
    )
