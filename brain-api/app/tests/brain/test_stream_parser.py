"""Incremental synthesizer-JSON parser tests.

The grounding verdict must land before any answer text (that ordering is what makes
token streaming safe), and the decoded answer must match ``json.loads`` exactly no
matter where chunk boundaries fall — including mid-escape and mid-surrogate-pair.
"""
from __future__ import annotations

import json

import pytest

from app.modules.brain.stream_parser import GroundedAnswerParser


def _payload(answer: str, *, grounded: bool = True, confidence: float = 0.82) -> str:
    """The synthesizer's contract: answer LAST, emitted in key order."""
    return (
        "{\n"
        f'  "grounded": {json.dumps(grounded)},\n'
        f'  "confidence": {confidence},\n'
        '  "usedSkillIds": ["skl_1"],\n'
        f"  \"answer\": {json.dumps(answer)}\n"
        "}"
    )


def _feed_in_chunks(raw: str, size: int) -> GroundedAnswerParser:
    parser = GroundedAnswerParser()
    for i in range(0, len(raw), size):
        parser.feed(raw[i : i + size])
    return parser


def test_head_resolves_before_any_answer_text() -> None:
    parser = GroundedAnswerParser()
    # Everything up to (not including) the answer key: no tokens may be emitted yet.
    emitted = parser.feed('{"grounded": true, "confidence": 0.82, "usedSkillIds": [],')
    assert emitted == ""
    assert parser.head is None  # answer key not seen yet
    emitted = parser.feed(' "answer": "Premium')
    assert parser.head == {"grounded": True, "confidence": 0.82, "usedSkillIds": []}
    assert emitted == "Premium"


def test_ungrounded_verdict_known_before_text_streams() -> None:
    parser = GroundedAnswerParser()
    parser.feed('{"grounded": false, "confidence": 0.0, "usedSkillIds": [], "answer": "')
    assert parser.head is not None
    assert parser.head["grounded"] is False
    assert parser.answer == ""  # caller can suppress tokens entirely


@pytest.mark.parametrize("size", [1, 2, 3, 5, 7, 13, 64, 4096])
def test_decoded_answer_matches_json_loads_at_every_chunk_size(size: int) -> None:
    answer = 'Refund "premium" customers within 45 days.\nSee: a\\b — 90% ✓'
    raw = _payload(answer)
    parser = _feed_in_chunks(raw, size)
    assert parser.complete
    assert parser.answer == json.loads(raw)["answer"] == answer


@pytest.mark.parametrize("size", [1, 2, 3, 6, 11])
def test_surrogate_pair_survives_chunk_splits(size: int) -> None:
    answer = "ship it \U0001f680 now"
    raw = (
        '{"grounded": true, "confidence": 1.0, "usedSkillIds": [], '
        f'"answer": "ship it \\ud83d\\ude80 now"}}'
    )
    parser = _feed_in_chunks(raw, size)
    assert parser.complete
    assert parser.answer == answer


def test_escape_split_across_chunk_boundary() -> None:
    parser = GroundedAnswerParser()
    parser.feed('{"grounded": true, "answer": "line1\\')
    # The lone backslash must be held back, not emitted as a literal.
    assert parser.answer == "line1"
    parser.feed('nline2"}')
    assert parser.answer == "line1\nline2"
    assert parser.complete


def test_unicode_escape_split_across_chunk_boundary() -> None:
    parser = GroundedAnswerParser()
    parser.feed('{"grounded": true, "answer": "90\\u00')
    assert parser.answer == "90"  # incomplete \uXXXX held back
    parser.feed('25 off"}')
    assert parser.answer == "90% off"


def test_tolerates_markdown_code_fence() -> None:
    raw = "```json\n" + _payload("Fenced answer.") + "\n```"
    parser = _feed_in_chunks(raw, 8)
    assert parser.head is not None
    assert parser.head["grounded"] is True
    assert parser.answer == "Fenced answer."


def test_stops_at_closing_quote_and_ignores_trailing_json() -> None:
    parser = GroundedAnswerParser()
    parser.feed(_payload("Done."))
    assert parser.complete
    assert parser.answer == "Done."
    # Trailing object syntax after the string must not leak into the answer.
    parser.feed('  , "extra": "ignored"}')
    assert parser.answer == "Done."


def test_empty_answer_is_complete_and_empty() -> None:
    parser = GroundedAnswerParser()
    parser.feed(_payload(""))
    assert parser.complete
    assert parser.answer == ""


def test_unparseable_head_degrades_to_empty_dict() -> None:
    parser = GroundedAnswerParser()
    parser.feed('not json at all "answer": "text"')
    assert parser.head == {}  # caller falls back rather than crashing
