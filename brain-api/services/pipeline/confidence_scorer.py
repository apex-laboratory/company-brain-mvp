_AUTHORITY_MULTIPLIERS: dict[str, float] = {
    "high": 1.0,
    "medium": 0.85,
    "low": 0.65,
}


def score(
    extraction_confidence: float,
    authority: str,
    sweep_sourced: bool = False,
    contradiction_detected: bool = False,
    human_authored: bool = False,
) -> float:
    """
    Pure function: compute final routing confidence score.

    Overrides (evaluated in order):
      human_authored=True      → 1.0  (always publishes, bypasses routing)
      contradiction_detected=True → 0.0  (forces review_queue)
      sweep_sourced=True       → score is computed normally but caller
                                  must route to review_queue regardless

    Formula: extraction_confidence × authority_multiplier
    """
    if human_authored:
        return 1.0
    if contradiction_detected:
        return 0.0
    multiplier = _AUTHORITY_MULTIPLIERS.get(authority, _AUTHORITY_MULTIPLIERS["low"])
    return extraction_confidence * multiplier
