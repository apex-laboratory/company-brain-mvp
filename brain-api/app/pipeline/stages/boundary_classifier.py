"""Boundary classifier (Gemini): how does a draft relate to existing skills?

First a pgvector similarity search (done by the caller via the repository); if
the closest match is at/below :data:`SIMILARITY_THRESHOLD` the draft is ``NEW``
with **no LLM call**. Above the threshold, Gemini picks one of
UPDATE / EXCEPTION / DUPLICATE / NEW against the top match.

Search scope is the caller's concern (published-only normally; published +
``review`` during sweeps, so a sweep's own pending skills dedupe against each
other — PRD sweep-scope rule).
"""
from __future__ import annotations

from app.pipeline.llm.clients import gemini_json
from app.pipeline.prompts import boundary_classifier as prompts
from app.pipeline.repository import SimilarSkill
from app.pipeline.types import (
    BOUNDARY_LABELS,
    SIMILARITY_THRESHOLD,
    BoundaryResult,
    SkillDraft,
    StageUsage,
)


async def classify_boundary(
    draft: SkillDraft, similar: list[SimilarSkill]
) -> tuple[BoundaryResult, StageUsage | None]:
    """Return ``(result, usage)``; usage is None on the no-LLM ``NEW`` path."""
    top = similar[0] if similar else None
    if top is None or top.similarity <= SIMILARITY_THRESHOLD:
        similarity = top.similarity if top else 0.0
        result = BoundaryResult(classification="NEW", matched_skill_id=None, similarity=similarity)
        return result, None

    parsed, usage = await gemini_json(
        prompts.SYSTEM,
        prompts.user_prompt(draft.trigger, draft.base_logic, top.name, top.base_logic),
        stage="boundary_classifier",
        max_tokens=256,
    )
    classification = str(parsed.get("classification", "")).upper()
    if classification not in BOUNDARY_LABELS:
        classification = "NEW"  # unparseable label → safest route (own skill, no mutation)
    return (
        BoundaryResult(
            classification=classification,
            matched_skill_id=top.id,
            similarity=top.similarity,
        ),
        usage,
    )
