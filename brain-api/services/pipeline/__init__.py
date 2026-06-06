from __future__ import annotations
from dataclasses import dataclass, field
from uuid import UUID


@dataclass
class SourceEvent:
    source: str
    event_type: str
    source_id: str
    payload: dict
    db_event_id: UUID | None = None
    sweep_id: UUID | None = None


@dataclass
class PipelineResult:
    outcome: str
    # published | queued | draft | discarded | duplicate | contradiction
    skill_id: UUID | None = None
    review_queue_id: UUID | None = None


async def run_pipeline(event: SourceEvent) -> PipelineResult:
    """
    Orchestrates the full extraction pipeline for a single source event.

    Flow:
      relevance_gate (Groq) → context_expanders → decision_identifier (Groq)
      → skill_extractor (Sonnet) → embedder → boundary_classifier (Groq)
      → contradiction_detector (Sonnet) → confidence_scorer → skill_writer
    """
    raise NotImplementedError("Phase 3")
