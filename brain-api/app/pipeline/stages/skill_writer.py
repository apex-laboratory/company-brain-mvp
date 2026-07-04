"""Skill writer: route a scored draft into skills/reviews (spec:
``services/skill_writer.py``, translated to the real schema).

Routing (thresholds from ``source_authority.yaml`` ``routing:``):

* ``conf >= 0.90`` AND authority >= medium AND not sweep_sourced
      → skill ``active`` + ``skill_versions`` (create, v1) + cache invalidate
      → event outcome ``published``
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

from app.pipeline import cache
from app.pipeline.authority import RoutingConfig
from app.pipeline.repository import PipelineRepository
from app.pipeline.types import DecisionMoment, PipelineResult, SkillDraft

_AUTHORITY_RANK = {"low": 0, "medium": 1, "high": 2}


def to_review_confidence(confidence: float) -> int:
    """Pipeline float 0–1 → reviews.confidence integer 0–100."""
    return round(max(0.0, min(1.0, confidence)) * 100)


def route(
    confidence: float, authority: str, sweep_sourced: bool, routing: RoutingConfig
) -> str:
    """Pure routing decision: 'published' | 'review' | 'draft'."""
    if confidence < routing.review_queue_confidence_floor:
        return "draft"
    meets_authority = _AUTHORITY_RANK.get(authority, 0) >= _AUTHORITY_RANK.get(
        routing.auto_publish_authority_floor, 1
    )
    if confidence >= routing.auto_publish_confidence and meets_authority and not sweep_sourced:
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
    confidence: float,
    authority: str,
    sweep_sourced: bool,
    routing: RoutingConfig,
    evidence: DecisionMoment | None,
) -> PipelineResult:
    """Insert a NEW skill routed by confidence. Runs inside the caller's
    tenant transaction; the caller commits and finalizes the event."""
    outcome = route(confidence, authority, sweep_sourced, routing)
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
        await cache.invalidate_skills(workspace_id)
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
            payload={
                "proposed_skill": {
                    "name": name,
                    "trigger": draft.trigger,
                    "base_logic": draft.base_logic,
                    "exceptions": draft.exceptions,
                    "actions": draft.actions,
                    "uncertainty_notes": draft.uncertainty_notes,
                },
                "boundary": "NEW",
            },
        )
        if sweep_id:
            await repo.bump_sweep_counters(session, sweep_id, queued=1)

    return PipelineResult(outcome=outcome, skill_id=skill_id, review_id=review_id)
