"""Live smoke test: a real call to the Gemini API using GEMINI_API_KEY from the env.

Unlike the rest of the suite (mocked at the ``app.pipeline.llm.clients``
boundary — see ``test_stages.py``), this hits the network for real. It exists
to answer one question fast: "is the configured key actually valid?" Skips
itself when no key is present or when ``LLM_PROVIDER`` isn't ``gemini``, so
the normal unit-test run never needs one.
"""
from __future__ import annotations

import pytest

from app.config.settings import settings
from app.pipeline.llm.clients import llm_json

pytestmark = pytest.mark.skipif(
    settings.llm_provider != "gemini" or not settings.gemini_api_key,
    reason="LLM_PROVIDER != gemini or GEMINI_API_KEY not set — skipping live Gemini call",
)


async def test_gemini_json_live_call() -> None:
    parsed, usage = await llm_json(
        "Respond with a single JSON object only, no prose.",
        'Return exactly this JSON object: {"ok": true}',
        stage="live_smoke_test",
        max_tokens=64,
    )
    assert parsed.get("ok") is True
    assert usage.model == settings.gemini_model
    assert usage.input_tokens > 0
    assert usage.output_tokens > 0
