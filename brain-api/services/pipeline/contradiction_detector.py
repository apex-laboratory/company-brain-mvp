from __future__ import annotations
from dataclasses import dataclass
from uuid import UUID
from .skill_extractor import SkillDraft


@dataclass
class ContradictionResult:
    has_contradiction: bool
    source_a: dict | None = None
    # {url, author, timestamp, excerpt, authority}
    source_b: dict | None = None
    # populated when has_contradiction=True


async def detect_contradiction(
    proposed: SkillDraft,
    existing_skill_id: UUID,
) -> ContradictionResult:
    """
    Sonnet: compare proposed change against the current published skill.

    If contradiction found:
      - Caller must NOT update the skill
      - Caller creates a review_queue row with review_type=contradiction
        and both source_a and source_b populated
      - source_events.outcome = contradiction
    """
    raise NotImplementedError("Phase 3")
