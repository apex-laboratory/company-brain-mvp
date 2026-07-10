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
