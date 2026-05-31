from __future__ import annotations
from dataclasses import dataclass
from uuid import UUID
from .skill_extractor import SkillDraft

BOUNDARY_LABELS = ("UPDATE", "EXCEPTION", "DUPLICATE", "NEW")
SIMILARITY_THRESHOLD = 0.82


@dataclass
class BoundaryResult:
    classification: str
    # UPDATE | EXCEPTION | DUPLICATE | NEW
    matched_skill_id: UUID | None
    similarity: float


async def classify_boundary(draft: SkillDraft) -> BoundaryResult:
    """
    Groq: run pgvector similarity search over published skills (top 3).

    If max_similarity > 0.82: ask Groq to classify the relationship as
    UPDATE / EXCEPTION / DUPLICATE / NEW.

    Below threshold: return NEW without an LLM call.

    Routes:
      UPDATE    → propose a version diff on base_logic, go to contradiction detector
      EXCEPTION → append row to exceptions_block, go to confidence scorer
      DUPLICATE → discard, log source as already covered
      NEW       → go to contradiction detector
    """
    raise NotImplementedError("Phase 3")
