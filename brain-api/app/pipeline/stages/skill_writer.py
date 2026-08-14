"""Skill writer: route a scored draft into skills/reviews (spec:
``services/skill_writer.py``, translated to the real schema).

Routing (threshold from ``source_authority.yaml`` ``routing:``):

* ``conf >= 0.70`` → skill ``review`` + ``reviews`` row → event outcome ``review``
* ``conf < 0.70`` → skill ``draft`` → event outcome ``draft``

There is no auto-publish branch: the only path to an ``active`` skill is a
reviewer approving the queued card (``reviews.service.approve``, which sets
confidence to 1.0 and writes the ``skill_versions`` row). Confidence decides
whether a human is *asked*, never whether one is *skipped*.
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.pipeline.authority import RoutingConfig
from app.pipeline.repository import PipelineRepository, SimilarSkill
from app.pipeline.types import DecisionMoment, PipelineResult, SkillDraft


def to_review_confidence(confidence: float) -> int:
    """Pipeline float 0–1 → reviews.confidence integer 0–100."""
    return round(max(0.0, min(1.0, confidence)) * 100)


def next_version(current: str) -> str:
    """'v1' → 'v2'. Falls back to 'v2' if the label isn't ``v<int>``."""
    try:
        return f"v{int(current.lstrip('v')) + 1}"
    except (ValueError, AttributeError):
        return "v2"


def route(confidence: float, routing: RoutingConfig) -> str:
    """Pure routing decision: 'review' | 'draft'."""
    if confidence < routing.review_queue_confidence_floor:
        return "draft"
    return "review"


async def write_new_skill(
    session: AsyncSession,
    repo: PipelineRepository,
    *,
    workspace_id: str,
    event_id: str,
    sweep_id: str | None,
    provider: str,
    source_url: str,
    draft: SkillDraft,
    embedding: list[float],
    embedding_model: str,
    confidence: float,
    authority: str,
    routing: RoutingConfig,
    evidence: DecisionMoment | None,
) -> PipelineResult:
    """Insert a NEW skill routed by confidence. Runs inside the caller's
    tenant transaction; the caller commits and finalizes the event.

    ``authority`` is still recorded on the row (``source_authority``) — it informs
    a reviewer and ranks contradiction sources; it just no longer gates routing.
    """
    outcome = route(confidence, routing)
    status = {"review": "review", "draft": "draft"}[outcome]

    name = await repo.resolve_skill_name(session, workspace_id, draft.name)
    skill_id = await repo.insert_skill(
        session,
        workspace_id=workspace_id,
        name=name,
        status=status,
        provider=provider,
        trigger=draft.trigger,
        base_logic=draft.base_logic,
        description=draft.uncertainty_notes or None,
        exceptions_block=draft.exceptions,
        actions=draft.actions,
        source_event_id=event_id,
        source_authority=authority,
        confidence=confidence,
        embedding=embedding,
        embedding_model=embedding_model,
    )

    review_id: str | None = None
    if outcome == "review":
        review_id = await repo.insert_review(
            session,
            workspace_id=workspace_id,
            title=name,
            kind="new_decision",
            provider=provider,
            source_location=source_url or None,
            before_text=None,
            after_text=draft.base_logic,
            evidence_quote=(evidence.decision_text[:500] if evidence else None),
            evidence_author=(evidence.author if evidence else None),
            confidence=to_review_confidence(confidence),
            skill_id=skill_id,
            payload={"proposed_skill": _proposed_skill(draft, name), "boundary": "NEW"},
        )
        if sweep_id:
            await repo.bump_sweep_counters(session, sweep_id, queued=1)

    return PipelineResult(outcome=outcome, skill_id=skill_id, review_id=review_id)


def _proposed_skill(draft: SkillDraft, name: str) -> dict:
    """The ``proposed_skill`` review-payload block (one source of truth for both the
    NEW payload and the matched-skill payload)."""
    return {
        "name": name,
        "trigger": draft.trigger,
        "base_logic": draft.base_logic,
        "exceptions": draft.exceptions,
        "actions": draft.actions,
        "uncertainty_notes": draft.uncertainty_notes,
    }


def _proposed_payload(draft: SkillDraft, name: str, boundary: str, matched_id: str) -> dict:
    return {
        "proposed_skill": _proposed_skill(draft, name),
        "matched_skill_id": matched_id,
        "boundary": boundary,
    }


async def _insert_matched_review(
    session: AsyncSession,
    repo: PipelineRepository,
    *,
    workspace_id: str,
    provider: str,
    source_url: str,
    matched: SimilarSkill,
    draft: SkillDraft,
    confidence: float,
    evidence: DecisionMoment | None,
    kind: str,
    boundary: str,
    sweep_id: str | None,
) -> PipelineResult:
    """Open a review against an existing skill (UPDATE/EXCEPTION) without mutating
    it — the change applies on approve. Shared by both matched-skill review branches."""
    review_id = await repo.insert_review(
        session,
        workspace_id=workspace_id,
        title=matched.name,
        kind=kind,
        provider=provider,
        source_location=source_url or None,
        before_text=matched.base_logic,
        after_text=draft.base_logic,
        evidence_quote=(evidence.decision_text[:500] if evidence else None),
        evidence_author=(evidence.author if evidence else None),
        confidence=to_review_confidence(confidence),
        skill_id=matched.id,
        payload=_proposed_payload(draft, matched.name, boundary, matched.id),
    )
    if sweep_id:
        await repo.bump_sweep_counters(session, sweep_id, queued=1)
    return PipelineResult(outcome="review", skill_id=matched.id, review_id=review_id)


async def write_duplicate(
    session: AsyncSession,
    repo: PipelineRepository,
    *,
    matched: SimilarSkill,
    event_id: str,
) -> PipelineResult:
    """DUPLICATE: no new skill — just record this event as another source of the
    matched skill and stop."""
    await repo.append_source_id(session, matched.id, event_id)
    return PipelineResult(outcome="duplicate", skill_id=matched.id)


async def write_update(
    session: AsyncSession,
    repo: PipelineRepository,
    *,
    workspace_id: str,
    provider: str,
    source_url: str,
    matched: SimilarSkill,
    draft: SkillDraft,
    confidence: float,
    sweep_id: str | None,
    routing: RoutingConfig,
    evidence: DecisionMoment | None,
) -> PipelineResult:
    """UPDATE: refine an existing skill's ``base_logic``.

    Never mutates the live skill — a ``policy_change`` review is queued and the
    change is applied on approve (M5).
    """
    outcome = route(confidence, routing)
    if outcome == "draft":
        return PipelineResult(outcome="draft", skill_id=matched.id)

    return await _insert_matched_review(
        session, repo, workspace_id=workspace_id, provider=provider, source_url=source_url,
        matched=matched, draft=draft, confidence=confidence, evidence=evidence,
        kind="policy_change", boundary="UPDATE", sweep_id=sweep_id,
    )


async def write_exception(
    session: AsyncSession,
    repo: PipelineRepository,
    *,
    workspace_id: str,
    provider: str,
    source_url: str,
    matched: SimilarSkill,
    draft: SkillDraft,
    confidence: float,
    sweep_id: str | None,
    routing: RoutingConfig,
    evidence: DecisionMoment | None,
) -> PipelineResult:
    """EXCEPTION: add a carve-out to an existing skill (base_logic unchanged).

    Never mutates the live skill — an ``exception`` review is queued and the
    carve-out is appended on approve (M5).
    """
    outcome = route(confidence, routing)
    if outcome == "draft":
        return PipelineResult(outcome="draft", skill_id=matched.id)

    return await _insert_matched_review(
        session, repo, workspace_id=workspace_id, provider=provider, source_url=source_url,
        matched=matched, draft=draft, confidence=confidence, evidence=evidence,
        kind="exception", boundary="EXCEPTION", sweep_id=sweep_id,
    )


async def write_contradiction(
    session: AsyncSession,
    repo: PipelineRepository,
    *,
    workspace_id: str,
    provider: str,
    matched: SimilarSkill,
    draft: SkillDraft,
    source_a: dict,
    source_b: dict,
) -> PipelineResult:
    """Contradiction: never mutate the skill — open a two-source review for a
    human to resolve. Confidence is 0 (the scorer forced it)."""
    review_id = await repo.insert_review(
        session,
        workspace_id=workspace_id,
        title=matched.name,
        kind="contradiction",
        provider=provider,
        source_location=source_b.get("url") or None,
        before_text=matched.base_logic,
        after_text=draft.base_logic,
        evidence_quote=source_b.get("excerpt"),
        evidence_author=source_b.get("author"),
        confidence=0,
        skill_id=matched.id,
        payload={
            "source_a": source_a,
            "source_b": source_b,
            # The newer rule's exceptions/actions, so approving (or picking source_b)
            # can replace the matched skill's clauses instead of leaving the
            # superseded ones attached to logic they no longer describe.
            "proposed_skill": _proposed_skill(draft, matched.name),
            "matched_skill_id": matched.id,
            "boundary": "UPDATE",
        },
    )
    return PipelineResult(outcome="contradiction", skill_id=matched.id, review_id=review_id)
