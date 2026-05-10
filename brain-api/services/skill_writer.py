async def write_skill(triple: dict, source_ids: list[str]) -> dict:
    """
    Write an extracted triple to the skills registry.
    Routes by confidence: >=0.9 → published, 0.7-0.89 → pending_review, <0.7 → draft.
    """
    raise NotImplementedError("Phase 3")
