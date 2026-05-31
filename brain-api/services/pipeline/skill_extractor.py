from __future__ import annotations
from dataclasses import dataclass, field
from .decision_identifier import DecisionMoment


@dataclass
class SkillDraft:
    trigger: str
    base_logic: str
    exceptions: list[dict]
    actions: list[dict]
    extraction_confidence: float
    uncertainty_notes: str = ""


async def extract_skill(
    decisions: list[DecisionMoment],
    context: str,
    authority: str,
) -> SkillDraft:
    """
    Sonnet Pass 2: extract a structured skill draft from decision moments
    and authority-annotated context.

    Instructs the model to:
      - Prioritize higher-authority sources when sources conflict
      - Express uncertainty rather than hallucinate (populate uncertainty_notes)
      - Produce extraction_confidence as a self-reported 0.0–1.0 float
    """
    raise NotImplementedError("Phase 3")
