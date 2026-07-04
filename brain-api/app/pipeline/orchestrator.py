"""Pipeline orchestrator: stage sequencing + cost ledger for one event.

Transaction discipline (plan decision #7): the event row is loaded in one short
tenant transaction, the LLM stages run with **no open transaction** (multi-second
calls must not hold a pooled connection), and all writes land in a single final
tenant transaction (skill/version/review + event finalize + commit).

Raises on unrecoverable pipeline failure (``LLMExhaustedError`` etc.) — the ARQ
task (`extract_event`) translates that into the dead-letter outcome ``failed``.
"""
from __future__ import annotations

import logging

from app.config.database import get_session
from app.integrations import get_integration
from app.integrations.base import RawEvent, RawItem
from app.pipeline import authority as authority_mod
from app.pipeline import confidence_scorer, embedder
from app.pipeline.repository import EventRow, PipelineRepository
from app.pipeline.stages import decision_identifier, relevance_gate, skill_extractor, skill_writer
from app.pipeline.types import CostLedger, PipelineResult
from app.shared.middleware.with_tenant import run_in_tenant

log = logging.getLogger(__name__)

_repo = PipelineRepository()


def _normalize(event: EventRow) -> RawEvent | None:
    """Re-derive the canonical RawEvent from the stored raw payload.

    ``source_events.payload`` holds the provider-shaped payload (``RawEvent.raw``);
    the connector's ``normalize`` recovers content/actor/url deterministically, so
    we don't persist the normalized form twice. ``None`` = no ingestible content.
    """
    integration = get_integration(event.provider)
    try:
        return integration.normalize(
            RawItem(external_id=event.external_event_id or "", payload=event.payload)
        )
    except ValueError:
        return None


async def _finalize(
    event: EventRow,
    *,
    outcome: str,
    skill_id: str | None,
    ledger: CostLedger,
    stage: str,
    extra_meta: dict | None = None,
) -> None:
    """Terminal write for non-writer outcomes (discard/duplicate/…)."""
    meta = {"stage": stage, "costs": ledger.as_meta(), **(extra_meta or {})}
    async with get_session() as session, run_in_tenant(
        session, event.workspace_id, "system", "admin"
    ):
        await _repo.finalize_event(
            session, event.id, outcome=outcome, skill_id=skill_id, pipeline_meta=meta
        )
        await session.commit()


async def run_pipeline(
    workspace_id: str, event_id: str, *, sweep_sourced: bool = False
) -> PipelineResult:
    """Process one ``source_events`` row through the full extraction pipeline."""
    ledger = CostLedger()

    # ── read transaction: load the event ────────────────────────────────────
    async with get_session() as session, run_in_tenant(
        session, workspace_id, "system", "admin"
    ):
        event = await _repo.load_event(session, event_id)
    if event is None:
        log.warning("pipeline: event %s not found", event_id)
        return PipelineResult(outcome="failed")
    if event.processed:
        log.info("pipeline: event %s already processed — skipping", event_id)
        return PipelineResult(outcome="skipped")

    # ── LLM stages (no open transaction) ─────────────────────────────────────
    raw = _normalize(event)
    if raw is None or not raw.content.strip():
        await _finalize(
            event, outcome="discarded", skill_id=None, ledger=ledger,
            stage="normalize", extra_meta={"reason": "no_content"},
        )
        return PipelineResult(outcome="discarded")

    relevant, reason, usage = await relevance_gate.is_relevant(raw.content, event.provider)
    ledger.add(usage)
    if not relevant:
        await _finalize(
            event, outcome="discarded", skill_id=None, ledger=ledger,
            stage="relevance_gate", extra_meta={"reason": reason},
        )
        return PipelineResult(outcome="discarded")

    # M4 hook: context expanders run here (post-gate, pre-identifier); until
    # then the raw normalized content is the context.
    context = raw.content

    decisions, id_usage = await decision_identifier.identify_decisions(
        context,
        event.provider,
        source_id=raw.source_id,
        author=str((raw.actor or {}).get("name") or (raw.actor or {}).get("id") or ""),
        timestamp=raw.created_at.isoformat(),
    )
    if id_usage:
        ledger.add(id_usage)
    if not decisions:
        await _finalize(
            event, outcome="discarded", skill_id=None, ledger=ledger,
            stage="decision_identifier", extra_meta={"reason": "no_decisions"},
        )
        return PipelineResult(outcome="discarded")

    annotation = authority_mod.annotate(event.provider, event.payload)

    try:
        draft, usage = await skill_extractor.extract_skill(decisions, context, annotation)
    except ValueError as exc:
        await _finalize(
            event, outcome="discarded", skill_id=None, ledger=ledger,
            stage="skill_extractor", extra_meta={"reason": str(exc)},
        )
        return PipelineResult(outcome="discarded")
    ledger.add(usage)

    embedding, usage = await embedder.embed_text(f"{draft.trigger}\n{draft.base_logic}")
    ledger.add(usage)

    confidence = confidence_scorer.score(
        draft.extraction_confidence, annotation.tier, sweep_sourced=sweep_sourced
    )
    routing = authority_mod.routing_config()

    # ── write transaction: skill/review + event finalize ─────────────────────
    async with get_session() as session, run_in_tenant(
        session, workspace_id, "system", "admin"
    ):
        result = await skill_writer.write_new_skill(
            session,
            _repo,
            workspace_id=workspace_id,
            event_id=event.id,
            sweep_id=event.sweep_id,
            provider=event.provider,
            source_url=raw.url,
            draft=draft,
            embedding=embedding,
            confidence=confidence,
            authority=annotation.tier,
            sweep_sourced=sweep_sourced,
            routing=routing,
            evidence=decisions[0] if decisions else None,
        )
        await _repo.finalize_event(
            session,
            event.id,
            outcome=result.outcome,
            skill_id=result.skill_id,
            pipeline_meta={"stage": "skill_writer", "costs": ledger.as_meta()},
        )
        await session.commit()

    log.info(
        "pipeline: event=%s outcome=%s cost_usd=%.6f stages=%d",
        event.id, result.outcome, ledger.total_usd, len(ledger.entries),
    )
    return result
