"""Unit tests for app/pipeline/confidence_scorer.py (PRD Feature 9)."""
from __future__ import annotations

import pytest

from app.pipeline.confidence_scorer import score


def test_high_authority_full_multiplier() -> None:
    assert score(0.9, "high") == pytest.approx(0.9)


def test_medium_authority_multiplier() -> None:
    assert score(0.9, "medium") == pytest.approx(0.765)


def test_low_authority_multiplier() -> None:
    assert score(0.9, "low") == pytest.approx(0.585)


def test_unknown_authority_treated_as_low() -> None:
    assert score(1.0, "wat") == pytest.approx(0.65)


def test_human_authored_always_one() -> None:
    assert score(0.1, "low", human_authored=True) == 1.0


def test_contradiction_forces_zero() -> None:
    assert score(0.99, "high", contradiction_detected=True) == 0.0


def test_human_authored_beats_contradiction() -> None:
    # Evaluated in order: human_authored wins.
    assert score(0.5, "low", contradiction_detected=True, human_authored=True) == 1.0


def test_sweep_sourced_does_not_change_score() -> None:
    # Sweep routing is the caller's job; the score itself is unchanged.
    assert score(0.9, "high", sweep_sourced=True) == score(0.9, "high")
