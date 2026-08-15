"""Relevance gate (LLM): binary — does this content contain operational
decision logic? False → the event is discarded (outcome='discarded')."""
from __future__ import annotations

from app.pipeline.llm.clients import llm_json
from app.pipeline.prompts import relevance_gate as prompts
from app.pipeline.types import StageUsage


async def is_relevant(content: str, provider: str) -> tuple[bool, str, StageUsage]:
    """Return ``(relevant, reason, usage)``."""
    parsed, usage = await llm_json(
        prompts.SYSTEM,
        prompts.user_prompt(content, provider),
        stage="relevance_gate",
        max_tokens=256,
    )
    return bool(parsed.get("relevant")), str(parsed.get("reason", "")), usage
