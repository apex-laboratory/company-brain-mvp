"""Live smoke test: a real call to the Groq API using GROQ_API_KEY from the env.

Unlike the rest of the suite (mocked at the ``app.pipeline.llm.clients``
boundary — see ``test_stages.py``), this hits the network for real. It exists
to answer one question fast: "is the configured key actually valid?" Skips
itself when no key is present, so the normal unit-test run never needs one.
"""
from __future__ import annotations

import pytest

from app.config.settings import settings
from app.pipeline.llm.clients import groq_json

pytestmark = pytest.mark.skipif(
    not settings.groq_api_key,
    reason="GROQ_API_KEY not set — skipping live Groq call",
)


async def test_groq_json_live_call() -> None:
    parsed, usage = await groq_json(
        "Respond with a single JSON object only, no prose.",
        'Return exactly this JSON object: {"ok": true}',
        stage="live_smoke_test",
        max_tokens=64,
    )
    assert parsed.get("ok") is True
    assert usage.model == settings.groq_model
    assert usage.input_tokens > 0
    assert usage.output_tokens > 0
