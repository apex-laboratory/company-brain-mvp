"""Contradiction detector (Groq): does a proposed change conflict with the
current published rule, or merely refine it?

Runs on the UPDATE route (and NEW-with-a-match). A true contradiction blocks the
skill write: the caller routes it to a ``contradiction`` review with both sources
instead of mutating the skill (confidence forced to 0.0 by the scorer).

Temporarily on Groq instead of Sonnet (ANTHROPIC_API_KEY unavailable) — swap
back to ``sonnet_json`` once the Anthropic key is restored.
"""
from __future__ import annotations

from app.pipeline.llm.clients import groq_json
from app.pipeline.prompts import contradiction_detector as prompts
from app.pipeline.repository import SimilarSkill
from app.pipeline.types import SkillDraft, StageUsage


async def detect_contradiction(
    proposed: SkillDraft, existing: SimilarSkill
) -> tuple[bool, StageUsage]:
    """Return ``(has_contradiction, usage)``."""
    parsed, usage = await groq_json(
        prompts.SYSTEM,
        prompts.user_prompt(proposed.base_logic, existing.base_logic),
        stage="contradiction_detector",
        max_tokens=512,
    )
    return bool(parsed.get("has_contradiction")), usage
