"""Eval suites: load labeled JSONL, run the real stage functions, score.

Each suite returns a :class:`SuiteReport` with per-item results and the metric
the PRD gates on. The runner never swallows item errors silently — an item whose
stage call raises is recorded as ``error`` and counts as a miss, because a prompt
that makes the model emit unparseable output *is* a quality regression.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path

from app.pipeline.repository import SimilarSkill
from app.pipeline.stages.boundary_classifier import classify_boundary
from app.pipeline.stages.contradiction_detector import detect_contradiction
from app.pipeline.stages.relevance_gate import is_relevant
from app.pipeline.types import SkillDraft

DATASET_DIR = Path(__file__).parent / "datasets"

# PRD Phase 3 acceptance gates.
RELEVANCE_PRECISION_GATE = 0.70
CONTRADICTION_RECALL_GATE = 0.80

_CONCURRENCY = 4  # be polite to provider rate limits


@dataclass
class ItemResult:
    id: str
    expected: object
    predicted: object
    correct: bool
    error: str | None = None


@dataclass
class SuiteReport:
    suite: str
    items: list[ItemResult] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)
    gates: dict[str, bool] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return all(self.gates.values())

    def as_dict(self) -> dict:
        return {
            "suite": self.suite,
            "metrics": {k: round(v, 4) for k, v in self.metrics.items()},
            "gates": self.gates,
            "passed": self.passed,
            "items": [
                {
                    "id": r.id,
                    "expected": r.expected,
                    "predicted": r.predicted,
                    "correct": r.correct,
                    **({"error": r.error} if r.error else {}),
                }
                for r in self.items
            ],
        }


def load_dataset(name: str, dataset_dir: Path = DATASET_DIR) -> list[dict]:
    path = dataset_dir / f"{name}.jsonl"
    rows = []
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    if not rows:
        raise ValueError(f"dataset {path} is empty")
    return rows


async def _gather_bounded(coros: list) -> list:
    sem = asyncio.Semaphore(_CONCURRENCY)

    async def _run(coro):
        async with sem:
            return await coro

    return await asyncio.gather(*(_run(c) for c in coros))


# ── relevance ─────────────────────────────────────────────────────────────────
async def run_relevance(dataset_dir: Path = DATASET_DIR) -> SuiteReport:
    """Precision on the positive ('relevant') class is the PRD gate: a false
    positive wastes two paid LLM passes downstream; a false negative only loses
    one candidate event."""
    rows = load_dataset("relevance", dataset_dir)

    async def _eval(row: dict) -> ItemResult:
        try:
            relevant, _reason, _usage = await is_relevant(row["content"], row["provider"])
            return ItemResult(row["id"], row["label"], relevant, relevant == row["label"])
        except Exception as exc:  # noqa: BLE001 — an unparseable prompt IS a failure
            return ItemResult(row["id"], row["label"], None, False, error=str(exc))

    report = SuiteReport("relevance", items=await _gather_bounded([_eval(r) for r in rows]))
    tp = sum(1 for r in report.items if r.predicted is True and r.expected is True)
    fp = sum(1 for r in report.items if r.predicted is True and r.expected is False)
    fn = sum(1 for r in report.items if r.predicted is not True and r.expected is True)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    accuracy = sum(1 for r in report.items if r.correct) / len(report.items)
    report.metrics = {"precision": precision, "recall": recall, "accuracy": accuracy}
    report.gates = {f"precision>={RELEVANCE_PRECISION_GATE}": precision >= RELEVANCE_PRECISION_GATE}
    return report


# ── boundary ──────────────────────────────────────────────────────────────────
def _similar_from_row(row: dict) -> list[SimilarSkill]:
    existing = row.get("existing")
    if existing is None:
        return []
    return [
        SimilarSkill(
            id=existing["id"],
            name=existing["name"],
            version="v1",
            base_logic=existing["base_logic"],
            exceptions_block=[],
            source_authority=None,
            similarity=float(row["similarity"]),
        )
    ]


async def run_boundary(dataset_dir: Path = DATASET_DIR) -> SuiteReport:
    """Overall four-way accuracy, plus per-label recall in the metrics. Reported,
    not gated — the PRD gates relevance precision and contradiction recall only."""
    rows = load_dataset("boundary", dataset_dir)

    async def _eval(row: dict) -> ItemResult:
        draft = SkillDraft(
            name=row["draft"]["name"],
            trigger=row["draft"]["trigger"],
            base_logic=row["draft"]["base_logic"],
            exceptions=[],
            actions=[],
            extraction_confidence=0.9,
        )
        try:
            result, _usage = await classify_boundary(draft, _similar_from_row(row))
            return ItemResult(
                row["id"], row["label"], result.classification,
                result.classification == row["label"],
            )
        except Exception as exc:  # noqa: BLE001
            return ItemResult(row["id"], row["label"], None, False, error=str(exc))

    report = SuiteReport("boundary", items=await _gather_bounded([_eval(r) for r in rows]))
    report.metrics = {"accuracy": sum(1 for r in report.items if r.correct) / len(report.items)}
    for label in ("NEW", "UPDATE", "DUPLICATE", "EXCEPTION"):
        of_label = [r for r in report.items if r.expected == label]
        if of_label:
            report.metrics[f"recall_{label.lower()}"] = sum(
                1 for r in of_label if r.correct
            ) / len(of_label)
    report.gates = {}  # measured, not gated (PRD gates cover relevance + contradiction)
    return report


# ── contradiction ─────────────────────────────────────────────────────────────
async def run_contradiction(dataset_dir: Path = DATASET_DIR) -> SuiteReport:
    """Recall on true contradictions is the PRD gate: a missed contradiction
    silently overwrites a live rule; a false alarm only costs one review card."""
    rows = load_dataset("contradiction", dataset_dir)

    def _skill(logic: str) -> SkillDraft:
        return SkillDraft(
            name="eval", trigger="eval", base_logic=logic,
            exceptions=[], actions=[], extraction_confidence=0.9,
        )

    def _existing(logic: str) -> SimilarSkill:
        return SimilarSkill(
            id="skill_eval", name="eval", version="v1", base_logic=logic,
            exceptions_block=[], source_authority=None, similarity=0.9,
        )

    async def _eval(row: dict) -> ItemResult:
        try:
            found, _usage = await detect_contradiction(
                _skill(row["proposed_logic"]), _existing(row["existing_logic"])
            )
            return ItemResult(row["id"], row["label"], found, found == row["label"])
        except Exception as exc:  # noqa: BLE001
            return ItemResult(row["id"], row["label"], None, False, error=str(exc))

    report = SuiteReport(
        "contradiction", items=await _gather_bounded([_eval(r) for r in rows])
    )
    tp = sum(1 for r in report.items if r.predicted is True and r.expected is True)
    fn = sum(1 for r in report.items if r.predicted is not True and r.expected is True)
    fp = sum(1 for r in report.items if r.predicted is True and r.expected is False)
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    accuracy = sum(1 for r in report.items if r.correct) / len(report.items)
    report.metrics = {"recall": recall, "precision": precision, "accuracy": accuracy}
    report.gates = {f"recall>={CONTRADICTION_RECALL_GATE}": recall >= CONTRADICTION_RECALL_GATE}
    return report


SUITES = {
    "relevance": run_relevance,
    "boundary": run_boundary,
    "contradiction": run_contradiction,
}


async def run_suites(
    names: list[str] | None = None, dataset_dir: Path = DATASET_DIR
) -> list[SuiteReport]:
    names = names or list(SUITES)
    unknown = set(names) - set(SUITES)
    if unknown:
        raise ValueError(f"unknown suites: {sorted(unknown)} (have {sorted(SUITES)})")
    return [await SUITES[name](dataset_dir) for name in names]


def format_report(reports: list[SuiteReport]) -> str:
    lines = []
    for rep in reports:
        gate_bits = [
            f"{name} {'PASS' if ok else 'FAIL'}" for name, ok in rep.gates.items()
        ] or ["(report-only)"]
        lines.append(f"── {rep.suite} ── {' · '.join(gate_bits)}")
        for key, value in rep.metrics.items():
            lines.append(f"   {key:<22} {value:.2%}")
        misses = [r for r in rep.items if not r.correct]
        for r in misses:
            detail = (
                f"error: {r.error}" if r.error
                else f"expected {r.expected!r}, got {r.predicted!r}"
            )
            lines.append(f"   ✗ {r.id}: {detail}")
        lines.append(f"   {len(rep.items) - len(misses)}/{len(rep.items)} correct")
        lines.append("")
    overall = all(r.passed for r in reports)
    lines.append(f"OVERALL: {'PASS' if overall else 'FAIL'}")
    return "\n".join(lines)
