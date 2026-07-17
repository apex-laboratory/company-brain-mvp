"""Reviews business logic — approve/reject a pipeline-extracted skill.

Approve applies the change the pipeline deferred (auto-publish only happens at
high confidence outside sweeps); the skill mutations reuse ``PipelineRepository``
so an approval produces exactly the same skill state as an auto-publish. Reject
records the verdict and demotes a review-only skill to ``draft`` so it stops
matching sweep-scope boundary searches.

Approving is a human confirmation, so the resulting skill's confidence is 1.0
(the ``human_authored`` semantics from PRD Feature 9 / skill_writer spec).
"""
from __future__ import annotations

from datetime import UTC, datetime

from app.config.database import get_session
from app.modules.reviews.repository import ReviewsRepository
from app.modules.reviews.schemas import ResolveResult, ReviewOut, ReviewStats
from app.pipeline import cache, embedder
from app.pipeline.repository import PipelineRepository
from app.pipeline.stages.skill_writer import next_version
from app.shared.errors.app_error import ConflictError, NotFoundError
from app.shared.middleware.authenticate import AuthContext
from app.shared.middleware.with_tenant import run_in_tenant

_HUMAN_CONFIDENCE = 1.0  # an approved skill is human-confirmed


def _require_workspace(auth: AuthContext) -> tuple[str, str]:
    if auth.workspace_id is None or auth.role is None:
        raise RuntimeError(
            "BUG: reviews service called without workspace context — "
            "ensure require_role is declared on this route"
        )
    return auth.workspace_id, auth.role


def _out(row: dict) -> ReviewOut:
    return ReviewOut(**{k: row.get(k) for k in ReviewOut.model_fields})


class ReviewsService:
    def __init__(
        self,
        repository: ReviewsRepository | None = None,
        skills: PipelineRepository | None = None,
    ) -> None:
        self._repo = repository or ReviewsRepository()
        self._skills = skills or PipelineRepository()

    async def list(
        self, auth: AuthContext, *, status: str | None, kind: str | None, limit: int
    ) -> list[ReviewOut]:
        workspace_id, role = _require_workspace(auth)
        async with get_session() as session, run_in_tenant(
            session, workspace_id, auth.user_id, role
        ):
            rows = await self._repo.list(session, status=status, kind=kind, limit=limit)
        return [_out(r) for r in rows]

    async def stats(self, auth: AuthContext) -> ReviewStats:
        workspace_id, role = _require_workspace(auth)
        async with get_session() as session, run_in_tenant(
            session, workspace_id, auth.user_id, role
        ):
            counts = await self._repo.stats(session)
        resolved = counts["approved"] + counts["rejected"]
        rate = counts["rejected"] / resolved if resolved else 0.0
        return ReviewStats(**counts, rejection_rate=round(rate, 4))

    async def approve(
        self, auth: AuthContext, review_id: str, comment: str | None
    ) -> ResolveResult:
        workspace_id, role = _require_workspace(auth)

        # Phase 1 (no write lock): read the pending review + skill and do any
        # network I/O (re-embedding) OUTSIDE a transaction — an embedding call must
        # never hold a pooled connection / open tenant transaction (orchestrator
        # rule), or an OpenAI outage pins them for the whole retry window.
        async with get_session() as session, run_in_tenant(
            session, workspace_id, auth.user_id, role
        ):
            review = await self._load_pending(session, review_id)
            skill_id = review["skill_id"]
            skill = await self._skills.get_skill(session, skill_id) if skill_id else None

        embedding: list[float] | None = None
        if skill is not None and review["kind"] in ("policy_change", "contradiction"):
            new_logic = review["after_text"] or skill["base_logic"]
            embedding, _ = await embedder.embed_text(f"{skill['trigger']}\n{new_logic}")

        # Phase 2 (write): lock the review row so a concurrent approve/reject
        # serializes behind us and then sees the resolved status (no double-apply).
        async with get_session() as session, run_in_tenant(
            session, workspace_id, auth.user_id, role
        ):
            review = await self._load_pending(session, review_id, for_update=True)
            if skill_id:
                await self._apply_approval(session, review, embedding=embedding)
            resolved = await self._repo.resolve(
                session, review_id, status="approved", verdict="approve",
                comment=comment, resolved_by=auth.user_id, resolved_at=datetime.now(UTC),
            )
            if not resolved:
                raise ConflictError("Review already resolved.")
            await session.commit()
        if skill_id:
            await cache.invalidate_skills(workspace_id)
        return ResolveResult(id=review_id, status="approved", verdict="approve", skill_id=skill_id)

    async def reject(
        self, auth: AuthContext, review_id: str, comment: str | None
    ) -> ResolveResult:
        workspace_id, role = _require_workspace(auth)
        async with get_session() as session, run_in_tenant(
            session, workspace_id, auth.user_id, role
        ):
            review = await self._load_pending(session, review_id, for_update=True)
            skill_id = review["skill_id"]
            # Only a new_decision's own review-status skill is demoted; UPDATE/
            # EXCEPTION/contradiction never mutated the live skill, so leave it.
            if skill_id and review["kind"] == "new_decision":
                skill = await self._skills.get_skill(session, skill_id)
                if skill and skill["status"] == "review":
                    await self._skills.set_skill_status(session, skill_id, "draft")
            resolved = await self._repo.resolve(
                session, review_id, status="rejected", verdict="reject",
                comment=comment, resolved_by=auth.user_id, resolved_at=datetime.now(UTC),
            )
            if not resolved:
                raise ConflictError("Review already resolved.")
            await session.commit()
        return ResolveResult(id=review_id, status="rejected", verdict="reject", skill_id=skill_id)

    # ── internals ──────────────────────────────────────────────────────────────

    async def _load_pending(
        self, session, review_id: str, *, for_update: bool = False
    ) -> dict:
        review = await self._repo.get(session, review_id, for_update=for_update)
        if review is None:
            raise NotFoundError("Review")
        if review["status"] != "pending":
            raise ConflictError(f"Review already {review['status']}.")
        return review

    async def _apply_approval(
        self, session, review: dict, *, embedding: list[float] | None
    ) -> None:
        """Mutate the skill per review kind (skill_id known to be set).

        ``embedding`` is precomputed by the caller for the policy_change/
        contradiction kinds (network I/O must stay out of this transaction)."""
        kind = review["kind"]
        skill_id = review["skill_id"]
        skill = await self._skills.get_skill(session, skill_id)
        if skill is None:  # skill deleted between extraction and approval
            return
        ws = skill["workspace_id"]

        if kind == "new_decision":
            # Skill already holds the extracted logic; confirm it (v1 version +
            # activate) and raise its confidence to the human-approved 1.0.
            await self._skills.insert_skill_version(
                session, workspace_id=ws, skill_id=skill_id, version=skill["version"] or "v1",
                base_logic=skill["base_logic"], exceptions_block=skill["exceptions_block"] or [],
                confidence=_HUMAN_CONFIDENCE, change_type="create",
            )
            await self._skills.set_skill_status(
                session, skill_id, "active", confidence=_HUMAN_CONFIDENCE
            )

        elif kind in ("policy_change", "contradiction"):
            # Apply the proposed base_logic (after_text) as a new version + re-embed.
            new_logic = review["after_text"] or skill["base_logic"]
            new_version = next_version(skill["version"])
            await self._skills.insert_skill_version(
                session, workspace_id=ws, skill_id=skill_id, version=new_version,
                base_logic=new_logic, exceptions_block=skill["exceptions_block"] or [],
                confidence=_HUMAN_CONFIDENCE, change_type="human_edit",
            )
            await self._skills.update_skill_logic(
                session, skill_id, base_logic=new_logic,
                exceptions_block=skill["exceptions_block"] or [], version=new_version,
                confidence=_HUMAN_CONFIDENCE, embedding=embedding, status="active",
            )

        elif kind == "exception":
            proposed_skill = (review.get("payload") or {}).get("proposed_skill") or {}
            proposed = proposed_skill.get("exceptions") or []
            if not proposed:
                # Mirror skill_writer.write_exception's auto-publish fallback so an
                # EXCEPTION with no explicit carve-outs still records one on approve
                # instead of silently dropping the boundary the pipeline detected.
                proposed = [
                    {
                        "condition": proposed_skill.get("trigger") or skill["trigger"],
                        "action": proposed_skill.get("base_logic") or skill["base_logic"],
                    }
                ]
            new_exceptions = (skill["exceptions_block"] or []) + proposed
            new_version = next_version(skill["version"])
            await self._skills.insert_skill_version(
                session, workspace_id=ws, skill_id=skill_id, version=new_version,
                base_logic=skill["base_logic"], exceptions_block=new_exceptions,
                confidence=_HUMAN_CONFIDENCE, change_type="human_edit",
            )
            await self._skills.apply_exception(
                session, skill_id, exceptions_block=new_exceptions, version=new_version,
                confidence=_HUMAN_CONFIDENCE,
            )
