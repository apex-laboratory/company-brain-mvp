from uuid import UUID
from services.pipeline.skill_extractor import SkillDraft


async def write_skill(
    draft: SkillDraft,
    source_event_id: UUID,
    authority: str,
    sweep_sourced: bool = False,
) -> dict:
    """
    Write an extracted skill draft to the skills registry.

    Routing by final confidence score:
      >= 0.90 AND authority >= medium AND sweep_sourced=False
        → status=published, version bump, Redis cache invalidate
      0.70–0.89 OR authority=low OR sweep_sourced=True
        → status=pending_review, write to review_queue
      < 0.70
        → status=draft, log to source_events only

    Always creates a skill_versions row when updating a published skill.
    Sets source_events.processed=True and source_events.outcome on completion.
    """
    raise NotImplementedError("Phase 3")
