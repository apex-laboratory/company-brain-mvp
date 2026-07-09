"""Eval-harness logic tests (Phase 3, M6).

The harness measures *prompt* quality against live LLMs, so its correctness —
dataset loading, metric math, gate evaluation, error accounting, and the no-LLM
boundary path — must be verified with fakes. A wrong precision formula would
pass or fail prompts silently.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.pipeline.stages import (
    boundary_classifier as bc,
)
from app.pipeline.stages import (
    contradiction_detector as cd,
)
from app.pipeline.stages import (
    relevance_gate as rg,
)
from app.pipeline.types import StageUsage
from evals.runner import (
    format_report,
    load_dataset,
    run_boundary,
    run_contradiction,
    run_relevance,
    run_suites,
)

_USAGE = StageUsage(stage="x", model="fake", input_tokens=1, output_tokens=1, cost_usd=0.0)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


@pytest.fixture
def dataset_dir(tmp_path: Path) -> Path:
    _write_jsonl(
        tmp_path / "relevance.jsonl",
        [
            {"id": "r1", "provider": "slack", "content": "REL yes", "label": True},
            {"id": "r2", "provider": "slack", "content": "REL yes", "label": True},
            {"id": "r3", "provider": "slack", "content": "chatter", "label": False},
            {"id": "r4", "provider": "slack", "content": "REL trick", "label": False},
        ],
    )
    _write_jsonl(
        tmp_path / "boundary.jsonl",
        [
            {  # below threshold → NEW without an LLM call
                "id": "b1", "label": "NEW", "similarity": 0.3,
                "draft": {"name": "a", "trigger": "t", "base_logic": "x"},
                "existing": {"id": "s1", "name": "s", "base_logic": "y"},
            },
            {  # no match at all → NEW without an LLM call
                "id": "b2", "label": "NEW", "similarity": 0.0,
                "draft": {"name": "b", "trigger": "t", "base_logic": "x"},
                "existing": None,
            },
            {  # above threshold → LLM label
                "id": "b3", "label": "UPDATE", "similarity": 0.9,
                "draft": {"name": "c", "trigger": "t", "base_logic": "x"},
                "existing": {"id": "s3", "name": "s", "base_logic": "y"},
            },
        ],
    )
    _write_jsonl(
        tmp_path / "contradiction.jsonl",
        [
            {"id": "c1", "label": True, "proposed_logic": "CONTRA a", "existing_logic": "b"},
            {"id": "c2", "label": True, "proposed_logic": "CONTRA c", "existing_logic": "d"},
            {"id": "c3", "label": False, "proposed_logic": "fine", "existing_logic": "e"},
        ],
    )
    return tmp_path


def _fake_groq(monkeypatch: pytest.MonkeyPatch) -> None:
    """Relevance: 'REL' in the user prompt → relevant. Boundary: always UPDATE."""

    async def fake(system: str, user: str, *, stage: str, max_tokens: int = 0):
        if stage == "relevance_gate":
            return {"relevant": "REL" in user, "reason": "fake"}, _USAGE
        return {"classification": "UPDATE"}, _USAGE

    monkeypatch.setattr(rg, "groq_json", fake)
    monkeypatch.setattr(bc, "groq_json", fake)


def _fake_sonnet(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(system: str, user: str, *, stage: str, max_tokens: int = 0):
        return {"has_contradiction": "CONTRA" in user}, _USAGE

    monkeypatch.setattr(cd, "sonnet_json", fake)


async def test_relevance_precision_recall_math(
    dataset_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Fake predicts True for r1, r2 (TP) and r4 (FP), False for r3 (TN).
    _fake_groq(monkeypatch)
    report = await run_relevance(dataset_dir)
    assert report.metrics["precision"] == pytest.approx(2 / 3)
    assert report.metrics["recall"] == 1.0
    assert report.metrics["accuracy"] == pytest.approx(3 / 4)
    # 2/3 < 0.70 → the PRD precision gate fails.
    assert report.passed is False


async def test_boundary_below_threshold_never_calls_llm(
    dataset_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    async def counting(system: str, user: str, *, stage: str, max_tokens: int = 0):
        calls.append(stage)
        return {"classification": "UPDATE"}, _USAGE

    monkeypatch.setattr(bc, "groq_json", counting)
    report = await run_boundary(dataset_dir)
    # b1 (0.3) and b2 (no match) resolve NEW with zero LLM calls; only b3 calls.
    assert calls == ["boundary_classifier"]
    assert report.metrics["accuracy"] == 1.0
    assert report.metrics["recall_new"] == 1.0
    assert report.metrics["recall_update"] == 1.0
    assert report.passed is True  # report-only suite: no gates to fail


async def test_contradiction_recall_gate(
    dataset_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_sonnet(monkeypatch)
    report = await run_contradiction(dataset_dir)
    assert report.metrics["recall"] == 1.0
    assert report.metrics["precision"] == 1.0
    assert report.passed is True


async def test_stage_error_counts_as_miss_not_crash(
    dataset_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def exploding(system: str, user: str, *, stage: str, max_tokens: int = 0):
        raise RuntimeError("model emitted garbage")

    monkeypatch.setattr(cd, "sonnet_json", exploding)
    report = await run_contradiction(dataset_dir)
    assert all(r.error for r in report.items)
    assert report.metrics["recall"] == 0.0
    assert report.passed is False  # recall gate fails — regression is visible


async def test_run_suites_rejects_unknown_names(dataset_dir: Path) -> None:
    with pytest.raises(ValueError, match="unknown suites"):
        await run_suites(["nonsense"], dataset_dir)


async def test_format_report_shows_gates_and_misses(
    dataset_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_groq(monkeypatch)
    _fake_sonnet(monkeypatch)
    reports = await run_suites(None, dataset_dir)
    text = format_report(reports)
    assert "── relevance ──" in text and "FAIL" in text  # precision gate fails (2/3)
    assert "✗ r4" in text  # the false positive is listed
    assert "OVERALL: FAIL" in text


def test_load_dataset_rejects_empty(tmp_path: Path) -> None:
    (tmp_path / "relevance.jsonl").write_text("\n")
    with pytest.raises(ValueError, match="empty"):
        load_dataset("relevance", tmp_path)


def test_shipped_datasets_are_valid() -> None:
    """The real datasets parse, have unique ids, and cover every label/class."""
    rel = load_dataset("relevance")
    bnd = load_dataset("boundary")
    con = load_dataset("contradiction")

    for rows in (rel, bnd, con):
        ids = [r["id"] for r in rows]
        assert len(ids) == len(set(ids)), "duplicate ids in dataset"

    assert {r["label"] for r in rel} == {True, False}
    assert {r["label"] for r in bnd} == {"NEW", "UPDATE", "DUPLICATE", "EXCEPTION"}
    assert {r["label"] for r in con} == {True, False}


def test_boundary_dataset_exercises_no_llm_path() -> None:
    from app.pipeline.types import SIMILARITY_THRESHOLD

    bnd = load_dataset("boundary")
    below = [r for r in bnd if r["similarity"] <= SIMILARITY_THRESHOLD]
    assert below, "boundary dataset must include below-threshold NEW cases"
    assert all(r["label"] == "NEW" for r in below)


def test_datasets_cover_github_engineering_knowledge() -> None:
    """The brain extracts engineering knowledge (review policies, deploy rules,
    rollback thresholds) from GitHub, not just business logic — the eval
    datasets must measure that, in both classes, in every suite."""
    rel = load_dataset("relevance")
    gh = [r for r in rel if r["provider"] == "github"]
    assert sum(1 for r in gh if r["label"]) >= 4, "need ≥4 relevant github items"
    assert sum(1 for r in gh if not r["label"]) >= 4, "need ≥4 irrelevant github items"

    # Engineering-flavored boundary cases across all four labels (bnd-15..18).
    bnd = load_dataset("boundary")
    eng = [r for r in bnd if r["existing"] and r["existing"]["id"].startswith("skill_g")]
    assert {r["label"] for r in eng} == {"NEW", "UPDATE", "DUPLICATE", "EXCEPTION"}

    # Engineering contradiction pairs in both classes (con-11..14).
    con = load_dataset("contradiction")
    eng_ids = {"con-11", "con-12", "con-13", "con-14"}
    eng_con = [r for r in con if r["id"] in eng_ids]
    assert {r["label"] for r in eng_con} == {True, False}
