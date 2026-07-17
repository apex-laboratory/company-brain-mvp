"""Skill extractor (Sonnet, Pass 2): decision moments + context → SkillDraft.

The prompt mandates expressing uncertainty over hallucinating and prioritizing
higher-authority statements on conflict. The model's self-reported
``extraction_confidence`` is clamped to [0, 1] before the confidence scorer
multiplies it by the authority tier.
"""
from __future__ import annotations

from app.pipeline.authority import AuthorityAnnotation
from app.pipeline.llm.clients import sonnet_json
from app.pipeline.prompts import skill_extractor as prompts
from app.pipeline.types import DecisionMoment, SkillDraft, StageUsage


def _decisions_block(decisions: list[DecisionMoment]) -> str:
    lines = []
    for i, d in enumerate(decisions, 1):
        signals = f" [{', '.join(d.signals)}]" if d.signals else ""
        lines.append(f"{i}. ({d.author} @ {d.timestamp}){signals}: {d.decision_text}")
    return "\n".join(lines)


def _clamp(value, default: float = 0.5) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


async def extract_skill(
    decisions: list[DecisionMoment],
    context: str,
    authority: AuthorityAnnotation,
) -> tuple[SkillDraft, StageUsage]:
    """Return ``(draft, usage)``. Raises ``ValueError`` if the model returned no
    usable skill (missing trigger/base_logic) — the caller discards the event."""
    parsed, usage = await sonnet_json(
        prompts.SYSTEM,
        prompts.user_prompt(
            _decisions_block(decisions), context, authority.tier, authority.weight
        ),
        stage="skill_extractor",
        max_tokens=2048,
    )

    trigger = str(parsed.get("trigger", "")).strip()
    base_logic = str(parsed.get("base_logic", "")).strip()
    if not trigger or not base_logic:
        raise ValueError("skill_extractor: model returned no trigger/base_logic")

    exceptions = parsed.get("exceptions")
    actions = parsed.get("actions")
    draft = SkillDraft(
        name=str(parsed.get("name", "")).strip() or trigger[:60],
        trigger=trigger,
        base_logic=base_logic,
        exceptions=[e for e in exceptions if isinstance(e, dict)]
        if isinstance(exceptions, list) else [],
        actions=[a for a in actions if isinstance(a, dict)]
        if isinstance(actions, list) else [],
        extraction_confidence=_clamp(parsed.get("extraction_confidence")),
        uncertainty_notes=str(parsed.get("uncertainty_notes", "")),
    )
    return draft, usage
