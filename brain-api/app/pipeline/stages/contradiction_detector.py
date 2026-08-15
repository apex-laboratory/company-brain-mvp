"""Contradiction detector (LLM): does a proposed change conflict with the
current published rule, or merely refine it?

A true contradiction blocks the skill write: the caller routes it to a
``contradiction`` review with both sources instead of writing or mutating a skill
(confidence forced to 0.0 by the scorer).

``screen`` is the entry point. It runs independently of the boundary
classification, because the two questions come apart: a draft can be a genuinely
NEW skill by the boundary's reckoning and still assert the opposite of a rule the
registry already publishes. Gating the check on ``UPDATE`` — which required
clearing :data:`SIMILARITY_THRESHOLD` — is what let two mutually-exclusive pairs
go live with empty ``conflict_flags``.
"""
from __future__ import annotations

from app.pipeline.llm.clients import llm_json
from app.pipeline.prompts import contradiction_detector as prompts
from app.pipeline.repository import SimilarSkill
from app.pipeline.types import CONTRADICTION_THRESHOLD, SkillDraft, StageUsage


async def detect_contradiction(
    proposed: SkillDraft, existing: SimilarSkill
) -> tuple[bool, StageUsage]:
    """Return ``(has_contradiction, usage)``."""
    parsed, usage = await llm_json(
        prompts.SYSTEM,
        prompts.user_prompt(proposed.base_logic, existing.base_logic),
        stage="contradiction_detector",
        max_tokens=512,
    )
    return bool(parsed.get("has_contradiction")), usage


async def screen(
    draft: SkillDraft, similar: list[SimilarSkill]
) -> tuple[SimilarSkill | None, list[StageUsage]]:
    """First skill in ``similar`` the draft genuinely contradicts, else ``None``.

    Returns ``(conflict, usages)``. ``similar`` arrives ordered by ascending vector
    distance, so the first candidate below :data:`CONTRADICTION_THRESHOLD` ends the
    scan — everything after it is further away still. Cost is therefore one LLM
    call per candidate in the band, short-circuited on the first real conflict, and
    zero when nothing is near enough to conflict with.
    """
    usages: list[StageUsage] = []
    for candidate in similar:
        if candidate.similarity < CONTRADICTION_THRESHOLD:
            break
        has_contradiction, usage = await detect_contradiction(draft, candidate)
        usages.append(usage)
        if has_contradiction:
            return candidate, usages
    return None, usages
