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

from app.config.database import get_tenant_session
from app.integrations import get_integration, is_threaded
from app.integrations.base import RawEvent, RawItem
from app.jobs.token_helper import token_for_connection, token_for_provider
from app.pipeline import authority as authority_mod
from app.pipeline import cache, confidence_scorer, embedder
from app.pipeline.expanders import ExpandRequest, get_expander, needs_expansion
from app.pipeline.repository import EventRow, PipelineRepository, SimilarSkill
from app.pipeline.stages import (
    boundary_classifier,
    contradiction_detector,
    decision_identifier,
    relevance_gate,
    skill_extractor,
    skill_writer,
)
from app.pipeline.types import CostLedger, PipelineResult, SkillDraft
from app.shared.middleware.with_tenant import run_in_tenant

log = logging.getLogger(__name__)

_repo = PipelineRepository()

# Boundary/contradiction search scope (plan cross-cutting #3): published set
# normally; during a sweep also match the sweep's own pending skills so repeated
# policy statements dedupe against each other instead of flooding the queue.
_PUBLISHED_SCOPE = ("active", "stable")
_SWEEP_SCOPE = ("active", "stable", "review")


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


async def _expand(event: EventRow, raw: RawEvent) -> tuple[str, str | None]:
    """Enrich the item with its surrounding context. Returns ``(text, error)``:
    ``text`` is the expanded context (or raw content on any miss), ``error`` a
    short reason string when expansion couldn't run (recorded in pipeline_meta,
    never fatal)."""
    if not needs_expansion(event.provider):
        return raw.content, None
    try:
        # Resolve the token from the connection that produced the event (stamped at
        # ingest). Legacy rows without it fall back to first-connection-per-provider.
        if event.source_connection_id:
            creds = await token_for_connection(
                event.workspace_id, event.source_connection_id
            )
        else:
            creds = await token_for_provider(event.workspace_id, event.provider)
        if creds is None:
            return raw.content, "no_connection"
        token, account_id = creds
        req = ExpandRequest(
            provider=event.provider,
            token=token,
            account_id=account_id,
            payload=event.payload,
            content=raw.content,
        )
        expanded = await get_expander(event.provider).expand(req)
        return (expanded.text or raw.content), None
    except Exception as exc:  # noqa: BLE001 — expansion is best-effort
        log.warning("pipeline: expander failed for %s (%s)", event.provider, exc)
        return raw.content, f"{type(exc).__name__}: {exc}"


def _source_dict(raw: RawEvent, authority: str, logic: str) -> dict:
    """Build a contradiction-card source: {url, author, timestamp, excerpt, authority}."""
    actor = raw.actor or {}
    return {
        "url": raw.url,
        "author": str(actor.get("name") or actor.get("id") or ""),
        "timestamp": raw.created_at.isoformat(),
        "excerpt": (logic or raw.content)[:500],
        "authority": authority,
    }


async def _existing_source_dict(
    workspace_id: str, matched: SimilarSkill
) -> dict:
    """Provenance of the existing skill (its latest source event), best-effort.

    Falls back to the skill's own fields when the originating event or its
    normalized form is unavailable.
    """
    fallback = {
        "url": "",
        "author": "",
        "timestamp": "",
        "excerpt": matched.base_logic[:500],
        "authority": matched.source_authority or "low",
    }
    async with get_tenant_session() as session, run_in_tenant(
        session, workspace_id, "system", "admin"
    ):
        prov = await _repo.skill_provenance(session, matched.id)
    if prov is None:
        return fallback
    raw = _normalize(prov)
    if raw is None:
        return fallback
    return _source_dict(raw, matched.source_authority or "low", matched.base_logic)


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
    async with get_tenant_session() as session, run_in_tenant(
        session, event.workspace_id, "system", "admin"
    ):
        await _repo.finalize_event(
            session, event.id, outcome=outcome, skill_id=skill_id, pipeline_meta=meta
        )
        await session.commit()


async def _commit(
    event: EventRow, ledger: CostLedger, make_result, *, extra_meta: dict | None = None
) -> PipelineResult:
    """Open the write transaction, run ``make_result(session)``, finalize + commit."""
    meta = {"stage": "skill_writer", "costs": ledger.as_meta(), **(extra_meta or {})}
    async with get_tenant_session() as session, run_in_tenant(
        session, event.workspace_id, "system", "admin"
    ):
        result = await make_result(session)
        await _repo.finalize_event(
            session,
            event.id,
            outcome=result.outcome,
            skill_id=result.skill_id,
            pipeline_meta=meta,
        )
        await session.commit()
    # Invalidate AFTER the commit, never before: an invalidation inside the write
    # transaction lets a concurrent reader miss the cache, read the still-committed
    # old skill, and repopulate it — serving the superseded rule until TTL. It also
    # keeps Redis I/O out of the pooled DB connection's transaction.
    # Currently unreachable — skill_writer.route no longer returns 'published', so
    # live invalidation happens on reviewer approve (reviews.service). Kept so
    # invalidation isn't silently missing if a publish path is ever reintroduced.
    if result.outcome == "published":
        await cache.invalidate_skills(event.workspace_id)
        # Keep the brain chat index fresh: re-embed the new/updated skill version.
        from app.modules.brain.reindex import schedule_reindex

        await schedule_reindex(event.workspace_id)
    return result


async def _route(
    event: EventRow,
    raw: RawEvent,
    draft: SkillDraft,
    embedding: list[float],
    embedding_model: str,
    annotation,
    routing,
    ledger: CostLedger,
    *,
    boundary,
    matched: SimilarSkill | None,
    conflict: SimilarSkill | None,
    evidence,
    extra_meta: dict | None = None,
) -> PipelineResult:
    """Dispatch a scored draft by its boundary classification.

    ``conflict`` outranks every boundary label. The other four outcomes are
    curation calls — where a piece of knowledge belongs — and a reviewer can undo
    any of them. A contradiction is a correctness problem: two rules that cannot
    both hold, which the brain would otherwise serve to agents with equal
    confidence. That has to reach a human before anything is written or mutated.
    """
    ws = event.workspace_id
    confidence = confidence_scorer.score(draft.extraction_confidence, annotation.tier)

    # Contradiction: never write or mutate a skill — open a two-source review.
    if conflict is not None:
        source_a = await _existing_source_dict(ws, conflict)
        source_b = _source_dict(raw, annotation.tier, draft.base_logic)
        return await _commit(
            event, ledger,
            lambda s: skill_writer.write_contradiction(
                s, _repo, workspace_id=ws, provider=event.provider, matched=conflict,
                draft=draft, source_a=source_a, source_b=source_b,
            ),
            extra_meta=extra_meta,
        )

    # DUPLICATE: record the event as another source of the matched skill, stop.
    if boundary.classification == "DUPLICATE" and matched is not None:
        return await _commit(
            event, ledger,
            lambda s: skill_writer.write_duplicate(
                s, _repo, provider=event.provider, matched=matched, event_id=event.id
            ),
            extra_meta=extra_meta,
        )

    # EXCEPTION: add a carve-out to the matched skill.
    if boundary.classification == "EXCEPTION" and matched is not None:
        return await _commit(
            event, ledger,
            lambda s: skill_writer.write_exception(
                s, _repo, workspace_id=ws, provider=event.provider, source_url=raw.url,
                matched=matched, draft=draft, confidence=confidence,
                sweep_id=event.sweep_id, routing=routing, evidence=evidence,
            ),
            extra_meta=extra_meta,
        )

    # UPDATE: refine the matched skill. The screen above already cleared it of any
    # contradiction, so reaching here means the draft genuinely refines the rule.
    if boundary.classification == "UPDATE" and matched is not None:
        return await _commit(
            event, ledger,
            lambda s: skill_writer.write_update(
                s, _repo, workspace_id=ws, provider=event.provider, source_url=raw.url,
                matched=matched, draft=draft, confidence=confidence,
                sweep_id=event.sweep_id, routing=routing, evidence=evidence,
            ),
            extra_meta=extra_meta,
        )

    # NEW (or no match): write a brand-new skill routed by confidence.
    return await _commit(
        event, ledger,
        lambda s: skill_writer.write_new_skill(
            s, _repo, workspace_id=ws, event_id=event.id, sweep_id=event.sweep_id,
            provider=event.provider, source_url=raw.url, draft=draft, embedding=embedding,
            embedding_model=embedding_model,
            confidence=confidence, authority=annotation.tier,
            routing=routing, evidence=evidence,
        ),
        extra_meta=extra_meta,
    )


async def run_pipeline(
    workspace_id: str, event_id: str, *, sweep_sourced: bool = False
) -> PipelineResult:
    """Process one ``source_events`` row through the full extraction pipeline."""
    ledger = CostLedger()

    # ── read transaction: load the event ────────────────────────────────────
    async with get_tenant_session() as session, run_in_tenant(
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
        return PipelineResult(outcome="discarded", cost_usd=ledger.total_usd)

    # Context expansion. Best-effort — a failure falls back to raw content and is
    # recorded, never fatal (PRD Phase 3).
    #
    # Threaded providers expand BEFORE the gate. The ingested event is only the
    # thread's parent message, and on a Q&A thread that parent is the question
    # ("ok to catch this exception and continue?") while the policy lives in a
    # reply. Gating on the parent alone discards the whole thread before its answer
    # is ever fetched, so the gate must see the expanded thread to judge it. Single-
    # document providers keep expanding after the gate, so an irrelevant page never
    # costs a fetch.
    expand_first = is_threaded(event.provider)
    context, expander_error = (
        await _expand(event, raw) if expand_first else (raw.content, None)
    )

    relevant, reason, usage = await relevance_gate.is_relevant(context, event.provider)
    ledger.add(usage)
    if not relevant:
        await _finalize(
            event, outcome="discarded", skill_id=None, ledger=ledger,
            stage="relevance_gate",
            extra_meta={
                "reason": reason,
                **({"expander_error": expander_error} if expander_error else {}),
            },
        )
        return PipelineResult(outcome="discarded", cost_usd=ledger.total_usd)

    if not expand_first:
        context, expander_error = await _expand(event, raw)
    expander_meta = {"expander_error": expander_error} if expander_error else None

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
            stage="decision_identifier",
            extra_meta={"reason": "no_decisions", **(expander_meta or {})},
        )
        return PipelineResult(outcome="discarded", cost_usd=ledger.total_usd)

    annotation = authority_mod.annotate(event.provider, event.payload)

    try:
        draft, usage = await skill_extractor.extract_skill(decisions, context, annotation)
    except ValueError as exc:
        await _finalize(
            event, outcome="discarded", skill_id=None, ledger=ledger,
            stage="skill_extractor",
            extra_meta={"reason": str(exc), **(expander_meta or {})},
        )
        return PipelineResult(outcome="discarded", cost_usd=ledger.total_usd)
    ledger.add(usage)

    embedding, usage = await embedder.embed_text(
        embedder.skill_embedding_text(draft.trigger, draft.base_logic)
    )
    embedding_model = usage.model  # stamped alongside the vector (migration 0018)
    ledger.add(usage)

    routing = authority_mod.routing_config()
    evidence = decisions[0] if decisions else None

    # ── boundary classification (pgvector search + optional Gemini) ───────────
    scope = _SWEEP_SCOPE if sweep_sourced else _PUBLISHED_SCOPE
    async with get_tenant_session() as session, run_in_tenant(
        session, workspace_id, "system", "admin"
    ):
        similar = await _repo.similar_skills(session, workspace_id, embedding, scope)
    boundary, b_usage = await boundary_classifier.classify_boundary(draft, similar)
    if b_usage:
        ledger.add(b_usage)

    matched = similar[0] if (boundary.matched_skill_id and similar) else None

    # Contradiction screen — deliberately NOT gated on the boundary label. A draft
    # the classifier calls NEW can still assert the opposite of a published rule;
    # contradictions embed further apart than paraphrases, so they rarely clear the
    # boundary threshold at all. DUPLICATE is the one skip: the draft restates a
    # skill already in the registry, so it introduces no claim that could conflict.
    if boundary.classification == "DUPLICATE":
        conflict, c_usages = None, []
    else:
        conflict, c_usages = await contradiction_detector.screen(draft, similar)
    for usage in c_usages:
        ledger.add(usage)

    result = await _route(
        event, raw, draft, embedding, embedding_model, annotation, routing, ledger,
        boundary=boundary, matched=matched, conflict=conflict, evidence=evidence,
        extra_meta=expander_meta,
    )

    result.cost_usd = ledger.total_usd
    log.info(
        "pipeline: event=%s outcome=%s boundary=%s cost_usd=%.6f stages=%d",
        event.id, result.outcome, boundary.classification, ledger.total_usd, len(ledger.entries),
    )
    return result


async def run_event_safely(
    workspace_id: str, event_id: str, *, sweep_sourced: bool = False
) -> PipelineResult:
    """Run the pipeline for one event, dead-lettering on any failure.

    Transient LLM errors were already retried inside ``with_retries``; anything
    still escaping ``run_pipeline`` marks the event ``outcome='failed'``
    (attempts+1, error recorded) and is swallowed — the single place the
    dead-letter contract (PRD Phase 3) lives, shared by ``extract_event`` and
    ``sweep_extract``.
    """
    try:
        return await run_pipeline(workspace_id, event_id, sweep_sourced=sweep_sourced)
    except Exception as exc:  # noqa: BLE001 — dead-letter by contract, never re-raise
        log.exception("pipeline: event %s failed — dead-lettered", event_id)
        try:
            async with get_tenant_session() as session, run_in_tenant(
                session, workspace_id, "system", "admin"
            ):
                await _repo.finalize_event(
                    session,
                    event_id,
                    outcome="failed",
                    pipeline_meta={"error": f"{type(exc).__name__}: {exc}"},
                )
                await session.commit()
        except Exception:  # noqa: BLE001
            log.exception("pipeline: could not record failure for %s", event_id)
        return PipelineResult(outcome="failed")
