"""AI provider factory (PRD: OpenRouter free-model testing/demo support).

One lazy SDK singleton per provider, mirroring ``http_client()`` in
``app/integrations/base.py``. ``clients.py`` never talks to an SDK directly —
it calls :func:`get_provider` and drives the returned :class:`LLMProvider`
through ``.call``/``.stream``, so retry/JSON-parse/cost logic stays provider-
agnostic. Swapping providers is a single env var (``LLM_PROVIDER``) — no
call-site changes.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol

from app.config.settings import settings


def _require_key(name: str, value: str) -> str:
    if not value:
        raise RuntimeError(
            f"{name} is not configured — the extraction pipeline needs it. "
            f"Set it in brain-api/.env."
        )
    return value


class LLMProvider(Protocol):
    """One chat-JSON call + one streaming call, both returning token usage."""

    model: str

    async def call(self, system: str, user: str, *, max_tokens: int) -> tuple[str, int, int]:
        """Return ``(text, input_tokens, output_tokens)``."""
        ...

    def stream(
        self, system: str, user: str, *, max_tokens: int
    ) -> AsyncIterator[str | tuple[int, int]]:
        """Yield text deltas, then one final ``(input_tokens, output_tokens)`` tuple."""
        ...


class GeminiProvider:
    def __init__(self) -> None:
        self._client: Any = None
        self.model = settings.gemini_model

    def _sdk(self) -> Any:
        if self._client is None:
            from google import genai

            self._client = genai.Client(
                api_key=_require_key("GEMINI_API_KEY", settings.gemini_api_key)
            )
        return self._client

    async def call(self, system: str, user: str, *, max_tokens: int) -> tuple[str, int, int]:
        from google.genai import types

        resp = await self._sdk().aio.models.generate_content(
            model=self.model,
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

    async def stream(
        self, system: str, user: str, *, max_tokens: int
    ) -> AsyncIterator[str | tuple[int, int]]:
        from google.genai import types

        in_tok = out_tok = 0
        stream = await self._sdk().aio.models.generate_content_stream(
            model=self.model,
            contents=user,
            config=types.GenerateContentConfig(
                system_instruction=system,
                temperature=0.0,
                max_output_tokens=max_tokens,
                response_mime_type="application/json",
            ),
        )
        async for chunk in stream:
            if chunk.text:
                yield chunk.text
            usage = chunk.usage_metadata
            if usage:
                in_tok = usage.prompt_token_count or in_tok
                out_tok = usage.candidates_token_count or out_tok
        yield (in_tok, out_tok)


class AnthropicProvider:
    def __init__(self) -> None:
        self._client: Any = None
        self.model = settings.anthropic_model

    def _sdk(self) -> Any:
        if self._client is None:
            import httpx
            from anthropic import AsyncAnthropic

            # SDK default is 600s + 2 internal retries; synthesis (2048 max_tokens)
            # finishes well inside 120s. Fail fast — with_retries owns retrying.
            self._client = AsyncAnthropic(
                api_key=_require_key("ANTHROPIC_API_KEY", settings.anthropic_api_key),
                timeout=httpx.Timeout(120.0, connect=5.0),
                max_retries=1,
            )
        return self._client

    async def call(self, system: str, user: str, *, max_tokens: int) -> tuple[str, int, int]:
        resp = await self._sdk().messages.create(
            model=self.model,
            system=system,
            messages=[{"role": "user", "content": user}],
            temperature=0.0,
            max_tokens=max_tokens,
        )
        text = "".join(block.text for block in resp.content if getattr(block, "text", None))
        return text, resp.usage.input_tokens, resp.usage.output_tokens

    async def stream(
        self, system: str, user: str, *, max_tokens: int
    ) -> AsyncIterator[str | tuple[int, int]]:
        async with self._sdk().messages.stream(
            model=self.model,
            system=system,
            messages=[{"role": "user", "content": user}],
            temperature=0.0,
            max_tokens=max_tokens,
        ) as stream:
            async for text in stream.text_stream:
                yield text
            final = await stream.get_final_message()
        yield (final.usage.input_tokens, final.usage.output_tokens)


class OpenRouterProvider:
    """OpenAI-compatible chat endpoint — reuses the ``openai`` SDK already used
    for embeddings, pointed at OpenRouter's ``base_url``.

    Deliberately does **not** pass ``response_format={"type": "json_object"}``:
    not every free model on OpenRouter supports strict JSON mode, and
    ``clients.py``'s markdown-fence-tolerant parser + one-reprompt-on-failure
    already handles a model that wraps JSON in prose.
    """

    def __init__(self) -> None:
        self._client: Any = None
        self.model = settings.openrouter_model

    def _sdk(self) -> Any:
        if self._client is None:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(
                api_key=_require_key("OPENROUTER_API_KEY", settings.openrouter_api_key),
                base_url=settings.openrouter_base_url,
            )
        return self._client

    async def call(self, system: str, user: str, *, max_tokens: int) -> tuple[str, int, int]:
        resp = await self._sdk().chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.0,
            max_tokens=max_tokens,
        )
        text = resp.choices[0].message.content or ""
        usage = resp.usage
        return (
            text,
            usage.prompt_tokens if usage else 0,
            usage.completion_tokens if usage else 0,
        )

    async def stream(
        self, system: str, user: str, *, max_tokens: int
    ) -> AsyncIterator[str | tuple[int, int]]:
        in_tok = out_tok = 0
        stream = await self._sdk().chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.0,
            max_tokens=max_tokens,
            stream=True,
            stream_options={"include_usage": True},
        )
        async for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content
            if chunk.usage:
                in_tok = chunk.usage.prompt_tokens or in_tok
                out_tok = chunk.usage.completion_tokens or out_tok
        yield (in_tok, out_tok)


_provider: LLMProvider | None = None
_provider_name: str | None = None

_PROVIDERS: dict[str, type] = {
    "gemini": GeminiProvider,
    "anthropic": AnthropicProvider,
    "openrouter": OpenRouterProvider,
}


def get_provider() -> LLMProvider:
    """Lazy singleton, rebuilt if ``settings.llm_provider`` changes (tests)."""
    global _provider, _provider_name
    if _provider is None or _provider_name != settings.llm_provider:
        _provider = _PROVIDERS[settings.llm_provider]()
        _provider_name = settings.llm_provider
    return _provider
