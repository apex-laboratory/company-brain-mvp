"""Skill writer: route a scored draft into skills/reviews (spec:
``services/skill_writer.py``, translated to the real schema).

Routing (thresholds from ``source_authority.yaml`` ``routing:``):

* ``conf >= 0.90`` AND authority >= medium AND not sweep_sourced
      → skill ``active`` + ``skill_versions`` (create, v1)
      → event outcome ``published`` (the orchestrator invalidates cache post-commit)
* ``0.70 <= conf`` (or low authority, or sweep_sourced)
      → skill ``review`` + ``reviews`` row (``new_decision``)
      → event outcome ``review``
* ``conf < 0.70`` → skill ``draft`` → event outcome ``draft``

Status mapping (skill_status enum is unchanged): published→active,
pending_review→review, draft→draft. The version row for review-routed skills is
created at approve time (M5), not here.
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.pipeline.authority import RoutingConfig
from app.pipeline.repository import PipelineRepository, SimilarSkill
from app.pipeline.types import DecisionMoment, PipelineResult, SkillDraft

_AUTHORITY_RANK = {"low": 0, "medium": 1, "high": 2}


def to_review_confidence(confidence: float) -> int:
    """Pipeline float 0–1 → reviews.confidence integer 0–100."""
    return round(max(0.0, min(1.0, confidence)) * 100)


def next_version(current: str) -> str:
    """'v1' → 'v2'. Falls back to 'v2' if the label isn't ``v<int>``."""
    try:
        return f"v{int(current.lstrip('v')) + 1}"
    except (ValueError, AttributeError):
        return "v2"


def route(
    confidence: float,
    authority: str,
    sweep_sourced: bool,
    routing: RoutingConfig,
    *,
    knowledge_type: str = "durable_policy",
) -> str:
    """Pure routing decision: 'published' | 'review' | 'draft'.

    Anything short of ``durable_policy`` (i.e. ``project_decision`` — release-
    scoped knowledge that expires) never auto-publishes: a human confirms it in
    review, same as sweep-sourced content.
    """
    if confidence < routing.review_queue_confidence_floor:
        return "draft"
    meets_authority = _AUTHORITY_RANK.get(authority, 0) >= _AUTHORITY_RANK.get(
        routing.auto_publish_authority_floor, 1
    )
    if (
        confidence >= routing.auto_publish_confidence
        and meets_authority
        and not sweep_sourced
        and knowledge_type == "durable_policy"
    ):
        return "published"
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
    sweep_sourced: bool,
    routing: RoutingConfig,
    evidence: DecisionMoment | None,
) -> PipelineResult:
    """Insert a NEW skill routed by confidence. Runs inside the caller's
    tenant transaction; the caller commits and finalizes the event."""
    outcome = route(
        confidence, authority, sweep_sourced, routing,
        knowledge_type=draft.knowledge_type,
    )
    status = {"published": "active", "review": "review", "draft": "draft"}[outcome]

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
    if outcome == "published":
        await repo.insert_skill_version(
            session,
            workspace_id=workspace_id,
            skill_id=skill_id,
            version="v1",
            base_logic=draft.base_logic,
            exceptions_block=draft.exceptions,
            confidence=confidence,
            change_type="create",
        )
    elif outcome == "review":
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
    embedding: list[float],
    embedding_model: str,
    confidence: float,
    authority: str,
    sweep_sourced: bool,
    sweep_id: str | None,
    routing: RoutingConfig,
    evidence: DecisionMoment | None,
) -> PipelineResult:
    """UPDATE: refine an existing skill's ``base_logic``.

    Publish branch mutates the skill (snapshot old → ``skill_versions``, bump
    version, re-embed, invalidate). Review branch writes a ``policy_change``
    review WITHOUT mutating the live skill — the change is applied on approve (M5).
    """
    outcome = route(
        confidence, authority, sweep_sourced, routing,
        knowledge_type=draft.knowledge_type,
    )
    new_version = next_version(matched.version)

    if outcome == "published":
        await repo.insert_skill_version(
            session,
            workspace_id=workspace_id,
            skill_id=matched.id,
            version=new_version,
            base_logic=draft.base_logic,
            exceptions_block=draft.exceptions or matched.exceptions_block,
            confidence=confidence,
            change_type="sweep_sourced" if sweep_sourced else "update",
        )
        await repo.update_skill_logic(
            session,
            matched.id,
            base_logic=draft.base_logic,
            exceptions_block=draft.exceptions or matched.exceptions_block,
            version=new_version,
            confidence=confidence,
            embedding=embedding,
            embedding_model=embedding_model,
        )
        return PipelineResult(outcome="published", skill_id=matched.id)

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
    authority: str,
    sweep_sourced: bool,
    sweep_id: str | None,
    routing: RoutingConfig,
    evidence: DecisionMoment | None,
) -> PipelineResult:
    """EXCEPTION: add a carve-out to an existing skill (base_logic unchanged).

    Publish branch appends to ``exceptions_block`` + version bump. Review branch
    writes an ``exception`` review; the append happens on approve (M5).
    """
    outcome = route(
        confidence, authority, sweep_sourced, routing,
        knowledge_type=draft.knowledge_type,
    )
    new_exceptions = list(matched.exceptions_block) + (
        draft.exceptions or [{"condition": draft.trigger, "action": draft.base_logic}]
    )

    if outcome == "published":
        new_version = next_version(matched.version)
        await repo.insert_skill_version(
            session,
            workspace_id=workspace_id,
            skill_id=matched.id,
            version=new_version,
            base_logic=matched.base_logic,
            exceptions_block=new_exceptions,
            confidence=confidence,
            change_type="sweep_sourced" if sweep_sourced else "update",
        )
        await repo.apply_exception(
            session,
            matched.id,
            exceptions_block=new_exceptions,
            version=new_version,
            confidence=confidence,
        )
        return PipelineResult(outcome="published", skill_id=matched.id)

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
            "matched_skill_id": matched.id,
            "boundary": "UPDATE",
        },
    )
    return PipelineResult(outcome="contradiction", skill_id=matched.id, review_id=review_id)
