"""Groq + Anthropic chat wrappers: JSON-mode calls with retry and cost capture.

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
from app.pipeline.llm.retry import with_retries
from app.pipeline.types import StageUsage

log = logging.getLogger(__name__)

_groq_client: Any = None
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


def groq_client() -> Any:
    """Lazy ``AsyncGroq`` singleton."""
    global _groq_client
    if _groq_client is None:
        from groq import AsyncGroq

        _groq_client = AsyncGroq(api_key=_require_key("GROQ_API_KEY", settings.groq_api_key))
    return _groq_client


def anthropic_client() -> Any:
    """Lazy ``AsyncAnthropic`` singleton."""
    global _anthropic_client
    if _anthropic_client is None:
        from anthropic import AsyncAnthropic

        _anthropic_client = AsyncAnthropic(
            api_key=_require_key("ANTHROPIC_API_KEY", settings.anthropic_api_key)
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


async def _groq_call(system: str, user: str, *, max_tokens: int) -> tuple[str, int, int]:
    resp = await groq_client().chat.completions.create(
        model=settings.groq_model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
        temperature=0.0,
        max_tokens=max_tokens,
    )
    usage = resp.usage
    return (
        resp.choices[0].message.content or "",
        usage.prompt_tokens if usage else 0,
        usage.completion_tokens if usage else 0,
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
    call, system: str, user: str, *, stage: str, model: str, max_tokens: int
) -> tuple[dict, StageUsage]:
    """Retry-wrapped call + JSON parse with a single reprompt on parse failure."""
    in_tok = out_tok = 0

    async def _attempt(prompt_user: str) -> tuple[str, int, int]:
        return await with_retries(
            lambda: call(system, prompt_user, max_tokens=max_tokens), stage=stage
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


async def groq_json(
    system: str, user: str, *, stage: str, max_tokens: int = 1024
) -> tuple[dict, StageUsage]:
    """Groq JSON-mode chat call for the fast classifier stages."""
    return await _json_call(
        _groq_call, system, user, stage=stage, model=settings.groq_model, max_tokens=max_tokens
    )


async def sonnet_json(
    system: str, user: str, *, stage: str, max_tokens: int = 2048
) -> tuple[dict, StageUsage]:
    """Sonnet chat call (JSON instructed via prompt) for the extraction stages."""
    return await _json_call(
        _sonnet_call,
        system,
        user,
        stage=stage,
        model=settings.anthropic_model,
        max_tokens=max_tokens,
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
