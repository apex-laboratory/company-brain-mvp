"""Pipeline data shapes (ported from the Phase-3 spec stubs in ``services/pipeline``).

These are in-memory DTOs passed between stages — they never leak to the API
layer. DB rows (skills, reviews, source_events) are written by the repository
from these shapes.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class StageUsage:
    """One LLM/embedding call's token usage + cost, tagged by pipeline stage."""

    stage: str  # relevance_gate | decision_identifier | skill_extractor | ...
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float


@dataclass
class CostLedger:
    """Accumulates per-stage usage across one event's pipeline run."""

    entries: list[StageUsage] = field(default_factory=list)

    def add(self, usage: StageUsage) -> None:
        self.entries.append(usage)

    @property
    def total_usd(self) -> float:
        return sum(e.cost_usd for e in self.entries)

    def by_stage(self) -> dict[str, float]:
        totals: dict[str, float] = {}
        for e in self.entries:
            totals[e.stage] = totals.get(e.stage, 0.0) + e.cost_usd
        return totals

    def as_meta(self) -> dict:
        """JSONB-ready shape for ``source_events.pipeline_meta['costs']``."""
        return {
            "total_usd": round(self.total_usd, 6),
            "by_stage": {k: round(v, 6) for k, v in self.by_stage().items()},
            "calls": [
                {
                    "stage": e.stage,
                    "model": e.model,
                    "input_tokens": e.input_tokens,
                    "output_tokens": e.output_tokens,
                    "cost_usd": round(e.cost_usd, 6),
                }
                for e in self.entries
            ],
        }


@dataclass
class DecisionMoment:
    """Pass 1 output: one authoritative decision moment inside threaded content."""

    message_id: str
    author: str
    timestamp: str
    decision_text: str
    signals: list[str] = field(default_factory=list)
    # e.g. ["definitive_language", "pinned", "positive_reactions", "authority_author"]


@dataclass
class SkillDraft:
    """Pass 2 output: a structured skill draft before boundary/confidence routing."""

    name: str  # short title; skills.name is NOT NULL + UNIQUE(workspace_id, name)
    trigger: str
    base_logic: str
    exceptions: list[dict]
    actions: list[dict]
    extraction_confidence: float  # model-self-reported, 0.0–1.0
    uncertainty_notes: str = ""


@dataclass
class ExpandedContext:
    """Context expander output: the full conversation/document around an event."""

    text: str
    participants: list[dict] = field(default_factory=list)
    reactions: list[dict] = field(default_factory=list)
    url: str = ""
    metadata: dict = field(default_factory=dict)


BOUNDARY_LABELS = ("UPDATE", "EXCEPTION", "DUPLICATE", "NEW")
SIMILARITY_THRESHOLD = 0.82


@dataclass
class BoundaryResult:
    """Four-way classification of a draft against existing similar skills."""

    classification: str  # UPDATE | EXCEPTION | DUPLICATE | NEW
    matched_skill_id: str | None
    similarity: float


@dataclass
class ContradictionResult:
    """Conflict comparison between a proposed change and the existing skill."""

    has_contradiction: bool
    source_a: dict | None = None  # {url, author, timestamp, excerpt, authority}
    source_b: dict | None = None


@dataclass
class PipelineResult:
    """Terminal result for one event.

    ``outcome`` is the canonical ``source_events.outcome`` terminal set:
    published | review | draft | discarded | duplicate | contradiction | failed
    """

    outcome: str
    skill_id: str | None = None
    review_id: str | None = None
