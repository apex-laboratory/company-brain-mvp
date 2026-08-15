"""Unit tests for app/pipeline/llm/pricing.py."""
from __future__ import annotations

import pytest

from app.pipeline.llm.pricing import cost_usd, ensure_priced


def test_gemini_pricing_math() -> None:
    # 1M in @ $0.10 + 1M out @ $0.40
    assert cost_usd("gemini-flash-lite-latest", 1_000_000, 1_000_000) == pytest.approx(0.50)


def test_sonnet_pricing_math() -> None:
    # 10k in @ $3/M + 2k out @ $15/M = 0.03 + 0.03
    assert cost_usd("claude-sonnet-5", 10_000, 2_000) == pytest.approx(0.06)


def test_embedding_pricing_input_only() -> None:
    assert cost_usd("text-embedding-3-small", 1_000_000, 0) == pytest.approx(0.02)


def test_unknown_model_costs_zero() -> None:
    assert cost_usd("some-future-model", 1_000_000, 1_000_000) == 0.0


def test_zero_tokens_zero_cost() -> None:
    assert cost_usd("claude-sonnet-5", 0, 0) == 0.0


def test_openrouter_free_model_costs_zero_without_warning() -> None:
    # Any ":free"-suffixed model is treated as an actual price guarantee, not
    # an unknown model — no _PRICES entry needed for it to work.
    assert cost_usd("deepseek/deepseek-chat-v3-0324:free", 1_000_000, 1_000_000) == 0.0


def test_ensure_priced_exempts_free_models() -> None:
    ensure_priced("claude-sonnet-5", "meta-llama/llama-3.3-70b-instruct:free")


def test_ensure_priced_raises_on_unpriced_non_free_model() -> None:
    with pytest.raises(RuntimeError, match="no price entry"):
        ensure_priced("some-future-paid-model")
