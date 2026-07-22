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

from app.config.database import get_tenant_session
from app.modules.reviews.repository import ReviewsRepository
from app.modules.reviews.schemas import (
    BulkApproveItem,
    BulkApproveResult,
    ContradictionResolveRequest,
    ResolveResult,
    ReviewOut,
    ReviewStats,
    WriteRequest,
)
from app.pipeline import cache, embedder
from app.pipeline.repository import PipelineRepository
from app.pipeline.stages.skill_writer import next_version
from app.shared.errors.app_error import (
    AppError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
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
        async with get_tenant_session() as session, run_in_tenant(
            session, workspace_id, auth.user_id, role
        ):
            rows = await self._repo.list(session, status=status, kind=kind, limit=limit)
        return [_out(r) for r in rows]

    async def stats(self, auth: AuthContext) -> ReviewStats:
        workspace_id, role = _require_workspace(auth)
        async with get_tenant_session() as session, run_in_tenant(
            session, workspace_id, auth.user_id, role
        ):
            counts = await self._repo.stats(session)
        resolved = counts["approved"] + counts["rejected"]
        rate = counts["rejected"] / resolved if resolved else 0.0
        return ReviewStats(**counts, rejection_rate=round(rate, 4))

    async def get(self, auth: AuthContext, review_id: str) -> ReviewOut:
        """One review with its full source context (the card the UI renders).

        For a contradiction the two conflicting sources live in ``payload``
        (``source_a``/``source_b``); ``_out`` passes ``payload`` through verbatim."""
        workspace_id, role = _require_workspace(auth)
        async with get_tenant_session() as session, run_in_tenant(
            session, workspace_id, auth.user_id, role
        ):
            review = await self._repo.get(session, review_id)
        if review is None:
            raise NotFoundError("Review")
        return _out(review)

    async def approve(
        self, auth: AuthContext, review_id: str, comment: str | None
    ) -> ResolveResult:
        workspace_id, role = _require_workspace(auth)

        # Phase 1 (no write lock): read the pending review + skill and do any
        # network I/O (re-embedding) OUTSIDE a transaction — an embedding call must
        # never hold a pooled connection / open tenant transaction (orchestrator
        # rule), or an OpenAI outage pins them for the whole retry window.
        async with get_tenant_session() as session, run_in_tenant(
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
        async with get_tenant_session() as session, run_in_tenant(
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
        async with get_tenant_session() as session, run_in_tenant(
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

    async def write(
        self, auth: AuthContext, review_id: str, body: WriteRequest
    ) -> ResolveResult:
        """Human writes the correct skill logic directly (PRD Feature 21 / Process 6).

        Publishes at confidence 1.0 regardless of the pipeline's routing — a human
        confirmation is the strongest signal we have. Re-embeds on the corrected
        logic so semantic search reflects the human's version."""
        return await self._apply_write(
            auth, review_id, base_logic=body.base_logic,
            exceptions=body.exceptions, comment=body.comment,
        )

    async def resolve_contradiction(
        self, auth: AuthContext, review_id: str, body: ContradictionResolveRequest
    ) -> ResolveResult:
        """Resolve a contradiction card (PRD Process 6).

        ``source_a`` adopts the current/higher-authority side (the review's
        ``before_text``); ``source_b`` adopts the newer conflicting side
        (``after_text``); ``write`` delegates to a human-authored correction.
        The winning side is applied as a new human-confirmed version."""
        # Peek the kind so we fail fast on a non-contradiction review before any
        # write; the authoritative pending-state check happens under the row lock.
        workspace_id, role = _require_workspace(auth)
        async with get_tenant_session() as session, run_in_tenant(
            session, workspace_id, auth.user_id, role
        ):
            review = await self._load_pending(session, review_id)
        if review["kind"] != "contradiction":
            raise ValidationError("Review is not a contradiction.")

        if body.choice == "write":
            if body.correction is None:
                raise ValidationError("A correction is required when choice is 'write'.")
            return await self._apply_write(
                auth, review_id, base_logic=body.correction.base_logic,
                exceptions=body.correction.exceptions, comment=body.comment,
            )

        # source_a → current logic (before_text); source_b → proposed (after_text).
        chosen = review["before_text"] if body.choice == "source_a" else review["after_text"]
        if not chosen:
            raise ValidationError(f"Contradiction has no text for {body.choice}.")
        return await self._apply_write(
            auth, review_id, base_logic=chosen, exceptions=None, comment=body.comment,
        )

    async def bulk_approve(
        self, auth: AuthContext, ids: list[str], comment: str | None
    ) -> BulkApproveResult:
        """Approve many reviews at once (PRD Feature 23 bulk sweep review).

        Each item runs through the single-item ``approve`` so it gets the same
        row-lock + skill-mutation guarantees; a per-item failure (already resolved,
        not found) is reported, never aborting the batch. De-dupes ids so a
        repeated id can't double-apply."""
        results: list[BulkApproveItem] = []
        for review_id in dict.fromkeys(ids):
            try:
                await self.approve(auth, review_id, comment)
                results.append(BulkApproveItem(id=review_id, status="approved"))
            except (ConflictError, NotFoundError) as exc:
                results.append(
                    BulkApproveItem(id=review_id, status="skipped", detail=exc.message)
                )
            except AppError as exc:  # unexpected but bounded — report, don't abort
                results.append(
                    BulkApproveItem(id=review_id, status="error", detail=exc.message)
                )
        approved = sum(1 for r in results if r.status == "approved")
        return BulkApproveResult(
            results=results, approved=approved, skipped=len(results) - approved
        )

    # ── internals ──────────────────────────────────────────────────────────────

    async def _apply_write(
        self,
        auth: AuthContext,
        review_id: str,
        *,
        base_logic: str,
        exceptions: list[dict] | None,
        comment: str | None,
    ) -> ResolveResult:
        """Shared human-authored publish path for ``write`` and contradiction picks.

        Two-phase like ``approve``: re-embed outside any transaction (network I/O
        must never pin a pooled connection), then apply + resolve under a row lock."""
        workspace_id, role = _require_workspace(auth)

        # Phase 1: read the pending review + skill; embed the corrected logic.
        async with get_tenant_session() as session, run_in_tenant(
            session, workspace_id, auth.user_id, role
        ):
            review = await self._load_pending(session, review_id)
            skill_id = review["skill_id"]
            if not skill_id:
                raise ValidationError("Review has no skill to write.")
            skill = await self._skills.get_skill(session, skill_id)
            if skill is None:
                raise NotFoundError("Skill")
        embedding, _ = await embedder.embed_text(f"{skill['trigger']}\n{base_logic}")

        # Phase 2: lock the review, apply the human version, resolve.
        async with get_tenant_session() as session, run_in_tenant(
            session, workspace_id, auth.user_id, role
        ):
            review = await self._load_pending(session, review_id, for_update=True)
            skill = await self._skills.get_skill(session, skill_id)
            if skill is None:
                raise NotFoundError("Skill")
            new_exceptions = (
                exceptions if exceptions is not None else (skill["exceptions_block"] or [])
            )
            new_version = next_version(skill["version"])
            await self._skills.insert_skill_version(
                session, workspace_id=skill["workspace_id"], skill_id=skill_id,
                version=new_version, base_logic=base_logic, exceptions_block=new_exceptions,
                confidence=_HUMAN_CONFIDENCE, change_type="human_edit",
            )
            await self._skills.update_skill_logic(
                session, skill_id, base_logic=base_logic, exceptions_block=new_exceptions,
                version=new_version, confidence=_HUMAN_CONFIDENCE, embedding=embedding,
                status="active",
            )
            resolved = await self._repo.resolve(
                session, review_id, status="approved", verdict="approve",
                comment=comment, resolved_by=auth.user_id, resolved_at=datetime.now(UTC),
            )
            if not resolved:
                raise ConflictError("Review already resolved.")
            await session.commit()
        await cache.invalidate_skills(workspace_id)
        return ResolveResult(
            id=review_id, status="approved", verdict="approve", skill_id=skill_id
        )

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
