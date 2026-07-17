"""Authority-weighted confidence scoring (ported verbatim from
``services/pipeline/confidence_scorer.py`` — PRD Feature 9).

These multipliers are the ROUTING math. The YAML ``tiers.*.weight`` values in
``source_authority.yaml`` are a prompt-weighting signal only (see
``app.pipeline.authority``).
"""
from __future__ import annotations

_AUTHORITY_MULTIPLIERS: dict[str, float] = {
    "high": 1.0,
    "medium": 0.85,
    "low": 0.65,
}


def score(extraction_confidence: float, authority: str) -> float:
    """Pure function: final routing confidence = extraction_confidence × authority
    multiplier.

    The two overrides this once carried live elsewhere, closer to their single
    source of truth, so they can't drift from this formula:
      * human approval → 1.0 directly in ``reviews.service`` (``_HUMAN_CONFIDENCE``)
      * detected contradiction → the draft never routes here; ``skill_writer`` opens
        a two-source review card at confidence 0
    ``sweep_sourced`` doesn't change the number (the caller routes a sweep to review
    regardless), so it's no longer a parameter.
    """
    multiplier = _AUTHORITY_MULTIPLIERS.get(authority, _AUTHORITY_MULTIPLIERS["low"])
    return extraction_confidence * multiplier
