"""Unit tests for app/pipeline/llm/providers.py — the AI provider factory.

Mocks at the SDK boundary so no network is touched.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.pipeline.llm.providers as providers
from app.pipeline.llm.providers import (
    AnthropicProvider,
    GeminiProvider,
    OpenRouterProvider,
    get_provider,
)

# ── key guards ───────────────────────────────────────────────────────────────

def test_gemini_missing_key_raises_at_first_use(monkeypatch) -> None:
    monkeypatch.setattr(
        providers, "settings", SimpleNamespace(gemini_api_key="", gemini_model="m")
    )
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        GeminiProvider()._sdk()


def test_anthropic_missing_key_raises_at_first_use(monkeypatch) -> None:
    monkeypatch.setattr(
        providers, "settings", SimpleNamespace(anthropic_api_key="", anthropic_model="m")
    )
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        AnthropicProvider()._sdk()


def test_openrouter_missing_key_raises_at_first_use(monkeypatch) -> None:
    monkeypatch.setattr(
        providers,
        "settings",
        SimpleNamespace(
            openrouter_api_key="", openrouter_model="m", openrouter_base_url="http://x"
        ),
    )
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        OpenRouterProvider()._sdk()


# ── get_provider factory ───────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("name", "cls"),
    [("gemini", GeminiProvider), ("anthropic", AnthropicProvider), ("openrouter", OpenRouterProvider)],
)
def test_get_provider_dispatches_on_llm_provider(monkeypatch, name, cls) -> None:
    monkeypatch.setattr(providers, "_provider", None)
    monkeypatch.setattr(providers, "_provider_name", None)
    monkeypatch.setattr(
        providers,
        "settings",
        SimpleNamespace(
            llm_provider=name,
            gemini_api_key="k", gemini_model="gm",
            anthropic_api_key="k", anthropic_model="am",
            openrouter_api_key="k", openrouter_model="om", openrouter_base_url="http://x",
        ),
    )
    assert isinstance(get_provider(), cls)


# ── OpenRouterProvider request shape ────────────────────────────────────────

async def test_openrouter_call_does_not_force_json_response_format() -> None:
    provider = OpenRouterProvider()
    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
        )
    )
    with patch.object(provider, "_sdk", return_value=fake_client):
        text, in_tok, out_tok = await provider.call("sys", "user", max_tokens=64)

    assert text == '{"ok": true}'
    assert (in_tok, out_tok) == (10, 5)
    kwargs = fake_client.chat.completions.create.call_args.kwargs
    assert "response_format" not in kwargs
    assert kwargs["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "user"},
    ]
