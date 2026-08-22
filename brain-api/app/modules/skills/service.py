"""Skills delivery business logic (Phase 5 — Features 12/14/15a, PRD §14).

Serves the agent-facing read surface — semantic search, full body, version
history, portable export — and the override feedback loop. Vector search reuses
the tested ``PipelineRepository.similar_skills`` HNSW query; the query embedding
is computed **outside** any transaction (network I/O must never pin a pooled
connection — the orchestrator/reviews rule). Every read runs RLS-scoped.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import io
import re
import zipfile
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import IntegrityError

from app.config.database import get_tenant_session
from app.modules.skills.repository import PUBLISHED_STATUSES, SkillsRepository
from app.modules.skills.schemas import (
    CreateSkillRequest,
    OverrideResult,
    SkillListItem,
    SkillOut,
    SkillSearchResult,
    SkillStats,
    SkillVersionOut,
    SubmitForReviewResult,
    UpdateSkillRequest,
)
from app.pipeline import cache, embedder
from app.pipeline.repository import PipelineRepository
from app.shared.errors.app_error import ConflictError, NotFoundError
from app.shared.middleware.authenticate import AuthContext
from app.shared.middleware.with_tenant import run_in_tenant

# Routing constants (mirror source_authority.yaml ``routing``).
_MATCH_THRESHOLD = 0.70  # below this, a query is "no_match" (Feature 15)
_AUTO_PUBLISH_FLOOR = 0.90  # below this, an override opens a review (Feature 15a)
_OVERRIDE_DECREMENT = 0.05


def _require_workspace(auth: AuthContext) -> tuple[str, str]:
    if auth.workspace_id is None or auth.role is None:
        raise RuntimeError(
            "BUG: skills service called without workspace context — "
            "ensure require_brain_access/require_role is declared on this route"
        )
    return auth.workspace_id, auth.role


class SkillsService:
    def __init__(
        self,
        repository: SkillsRepository | None = None,
        pipeline: PipelineRepository | None = None,
    ) -> None:
        self._repo = repository or SkillsRepository()
        self._pipeline = pipeline or PipelineRepository()

    async def search(
        self, auth: AuthContext, query: str, limit: int
    ) -> list[SkillSearchResult]:
        """Semantic search over published skills; logs the interaction."""
        workspace_id, role = _require_workspace(auth)
        embedding, _ = await embedder.embed_text(query, interactive=True)
        async with get_tenant_session() as session, run_in_tenant(
            session, workspace_id, auth.user_id, role
        ):
            hits = await self._pipeline.similar_skills(
                session, workspace_id, embedding, PUBLISHED_STATUSES, limit=limit
            )
            top = hits[0] if hits else None
            matched = top is not None and top.similarity >= _MATCH_THRESHOLD
            await self._repo.insert_interaction(
                session, workspace_id=workspace_id, user_id=auth.user_id,
                skill_id=top.id if matched else None, query=query,
                matched_confidence=top.similarity if top else None,
                match_type="semantic" if matched else "no_match",
            )
            # calls_30d/updated_at ride along on the vector query itself —
            # no second SELECT over the rows we just retrieved.
            series = await self._repo.call_series(session, [h.id for h in hits])
            await session.commit()
        return [
            SkillSearchResult(
                id=h.id, name=h.name, version=h.version, status=h.status,
                base_logic=h.base_logic, exceptions_block=h.exceptions_block,
                source_authority=h.source_authority, source_providers=h.source_providers,
                similarity=round(h.similarity, 4),
                calls30d=h.calls_30d,
                call_series=_densify_series(series.get(h.id, {})),
                updated_at=h.updated_at,
            )
            for h in hits
        ]

    async def list(
        self,
        auth: AuthContext,
        *,
        status: str | None,
        source: str | None,
        limit: int,
        cursor: str | None,
    ) -> tuple[list[SkillListItem], str | None]:
        """Browse the registry without a query (``GET /skills``).

        Returns a page plus the next cursor (``None`` when exhausted). A 7-point
        daily ``callSeries`` is attached per row from one batched interactions query."""
        workspace_id, role = _require_workspace(auth)
        statuses = (status,) if status is not None else None
        decoded = _decode_cursor(cursor)
        async with get_tenant_session() as session, run_in_tenant(
            session, workspace_id, auth.user_id, role
        ):
            rows = await self._repo.list_skills(
                session, statuses=statuses, source=source, limit=limit + 1, cursor=decoded
            )
            has_more = len(rows) > limit
            page = rows[:limit]
            series = await self._repo.call_series(session, [r["id"] for r in page])
        items = [
            SkillListItem(
                id=r["id"], name=r["name"], version=r["version"], status=r["status"],
                base_logic=r["base_logic"], exceptions_block=r["exceptions_block"] or [],
                source_authority=r["source_authority"],
                source_providers=r["source_providers"] or [],
                description=r["description"], calls30d=int(r["calls_30d"]),
                call_series=_densify_series(series.get(r["id"], {})),
                updated_at=r["updated_at"],
            )
            for r in page
        ]
        next_cursor = (
            _encode_cursor(page[-1]["updated_at"], page[-1]["id"])
            if has_more and page
            else None
        )
        return items, next_cursor

    async def stats(self, auth: AuthContext) -> SkillStats:
        """Registry summary strip (mirrors ``ReviewsService.stats``)."""
        workspace_id, role = _require_workspace(auth)
        async with get_tenant_session() as session, run_in_tenant(
            session, workspace_id, auth.user_id, role
        ):
            counts = await self._repo.stats(session)
        return SkillStats(
            total=counts["total"],
            stable=counts["stable"] + counts["active"],  # both are agent-queryable
            in_review=counts["review"],
            draft=counts["draft"],
            calls30d=counts["calls30d"],
        )

    async def create(self, auth: AuthContext, req: CreateSkillRequest) -> SkillOut:
        """Manually author a skill (admin-only): insert a draft and open a review.

        The embedding is computed **before** the transaction (network I/O must never
        pin a pooled connection). The draft is not agent-queryable until a human
        approves the review it opens."""
        workspace_id, role = _require_workspace(auth)
        embedding, usage = await embedder.embed_text(
            embedder.skill_embedding_text(req.trigger, req.base_logic)
        )
        async with get_tenant_session() as session, run_in_tenant(
            session, workspace_id, auth.user_id, role
        ):
            try:
                skill_id = await self._repo.insert_draft(
                    session, workspace_id=workspace_id, name=req.name,
                    trigger=req.trigger, base_logic=req.base_logic,
                    description=req.description, embedding=embedding,
                    embedding_model=usage.model,
                )
            except IntegrityError as exc:
                raise ConflictError("A skill with this name already exists.") from exc
            await self._pipeline.insert_review(
                session, workspace_id=workspace_id, title=req.name,
                kind="new_decision", provider=None, source_location=None,
                before_text=None, after_text=req.base_logic,
                evidence_quote=None, evidence_author=None, confidence=100,
                skill_id=skill_id, payload={"authoredBy": auth.user_id},
            )
            skill = await self._repo.get(session, skill_id)
            await session.commit()
        return SkillOut(**skill)

    async def update(
        self, auth: AuthContext, skill_id: str, req: UpdateSkillRequest
    ) -> SkillOut:
        """Edit an existing skill from the dashboard (editor or admin).

        Partial: only the fields present in the request are written. Editing the
        ``trigger`` or ``base_logic`` changes what the skill *means*, so its vector
        is recomputed — otherwise semantic search would keep matching the old rule.
        The embedding is computed **before** the write transaction (network I/O must
        never pin a pooled connection — the create/reviews rule) and merged with the
        current values so a one-field edit still embeds the whole skill text.

        404 when the skill is unknown or soft-deleted; a duplicate ``name`` maps to
        409. The read cache is invalidated because a served answer may now be stale."""
        workspace_id, role = _require_workspace(auth)
        provided = req.model_dump(exclude_unset=True)
        if not provided:
            # Empty body — nothing to change. Return the current body rather than
            # touching updated_at, so a no-op edit doesn't look like a real change.
            return await self.get_any(auth, skill_id)

        current = await self.get_any(auth, skill_id)  # 404s if missing/deleted

        embedding: list[float] | None = None
        embedding_model: str | None = None
        if "trigger" in provided or "base_logic" in provided:
            trigger = provided.get("trigger", current.trigger)
            base_logic = provided.get("base_logic", current.base_logic)
            emb, usage = await embedder.embed_text(
                embedder.skill_embedding_text(trigger, base_logic)
            )
            embedding, embedding_model = emb, usage.model

        async with get_tenant_session() as session, run_in_tenant(
            session, workspace_id, auth.user_id, role
        ):
            try:
                updated = await self._repo.update(
                    session, skill_id, fields=provided, changed_by=auth.user_id,
                    embedding=embedding, embedding_model=embedding_model,
                )
            except IntegrityError as exc:
                raise ConflictError("A skill with this name already exists.") from exc
            if updated is None:
                raise NotFoundError("Skill")  # raced with a delete between reads
            await session.commit()
        await cache.invalidate_skills(workspace_id)
        return SkillOut(**updated)

    async def delete(self, auth: AuthContext, skill_id: str) -> None:
        """Soft-delete a skill (admin-only). Idempotent-friendly: a 404 is raised
        only when nothing matched, so the caller learns the row was already gone.

        Invalidates the read cache so ``query_brain`` stops serving a now-deleted
        skill from a warm entry."""
        workspace_id, role = _require_workspace(auth)
        async with get_tenant_session() as session, run_in_tenant(
            session, workspace_id, auth.user_id, role
        ):
            deleted = await self._repo.soft_delete(session, skill_id)
            if not deleted:
                raise NotFoundError("Skill")
            await session.commit()
        await cache.invalidate_skills(workspace_id)

    async def get_any(self, auth: AuthContext, skill_id: str) -> SkillOut:
        """Full body of a skill in **any** status (draft/review/published).

        Unlike ``get`` (agent surface, published-only) the dashboard edit/delete
        flow must reach drafts and in-review rows too. 404s when unknown or
        soft-deleted."""
        workspace_id, role = _require_workspace(auth)
        async with get_tenant_session() as session, run_in_tenant(
            session, workspace_id, auth.user_id, role
        ):
            skill = await self._repo.get(session, skill_id)
        if skill is None:
            raise NotFoundError("Skill")
        return SkillOut(**skill)

    async def submit_for_review(
        self, auth: AuthContext, skill_id: str, note: str | None
    ) -> SubmitForReviewResult:
        """Move a draft skill into the review queue (``draft`` → ``review``).

        Drafts land two ways — the pipeline routes a low-confidence extraction
        there, and a reviewer's reject demotes a review-only skill back — and until
        now nothing could put one in front of a human again. This promotes the skill
        and opens the ``new_decision`` card the queue renders; approving it publishes
        through the existing ``POST /reviews/{id}/approve`` path.

        No embedding is recomputed: the trigger/logic are unchanged, so the vector
        written at draft time is still correct and this stays a single transaction
        with no network I/O inside it."""
        workspace_id, role = _require_workspace(auth)
        async with get_tenant_session() as session, run_in_tenant(
            session, workspace_id, auth.user_id, role
        ):
            skill = await self._repo.get(session, skill_id)
            if skill is None:
                raise NotFoundError("Skill")
            if skill["status"] != "draft":
                raise ConflictError(
                    f"Only a draft skill can be submitted for review "
                    f"(this one is '{skill['status']}')."
                )
            if not await self._repo.promote_draft_to_review(session, skill_id):
                # Lost the race with a concurrent submit (or a delete) — the other
                # caller owns the review row; don't open a second one.
                raise ConflictError("Skill is no longer a draft.")

            review_id = await self._repo.pending_review_id(session, skill_id)
            review_created = review_id is None
            if review_id is None:
                review_id = await self._pipeline.insert_review(
                    session, workspace_id=workspace_id, title=skill["name"],
                    kind="new_decision", provider=None, source_location=None,
                    before_text=None, after_text=skill["base_logic"],
                    evidence_quote=None, evidence_author=None,
                    confidence=_review_confidence(skill["confidence"]),
                    skill_id=skill_id,
                    payload={"submittedBy": auth.user_id, "note": note},
                )
            await session.commit()
        return SubmitForReviewResult(
            skill_id=skill_id, status="review",
            review_id=review_id, review_created=review_created,
        )

    async def query(self, auth: AuthContext, situation: str) -> dict:
        """The ``query_brain`` core (PRD Feature 15 / Process 3).

        Cache check → semantic search → best published match, or a ``no_match``
        the caller may escalate to query-driven extraction. Every call logs an
        ``agent_interactions`` row and returns its ``interaction_id`` so the agent
        can later report an override. Only semantic matches are cached (a cached
        ``no_match`` would hide a skill added within the TTL)."""
        workspace_id, role = _require_workspace(auth)

        cached = await cache.get_cached_search(workspace_id, situation)
        if cached is not None:
            interaction_id = await self._log_interaction(
                auth, situation,
                skill_id=cached.get("skill_id"),
                confidence=cached.get("similarity_score"),
                match_type=cached["match_type"],
            )
            return {**cached, "interaction_id": interaction_id, "cache_hit": True}

        embedding, _ = await embedder.embed_text(situation, interactive=True)
        async with get_tenant_session() as session, run_in_tenant(
            session, workspace_id, auth.user_id, role
        ):
            hits = await self._pipeline.similar_skills(
                session, workspace_id, embedding, PUBLISHED_STATUSES, limit=5
            )
            top = hits[0] if hits else None
            full = None
            if top is not None and top.similarity >= _MATCH_THRESHOLD:
                full = await self._repo.get(session, top.id, statuses=PUBLISHED_STATUSES)

            if full is not None:
                response = _semantic_response(full, top.similarity)
            else:
                response = {
                    "match_type": "no_match",
                    "skill_name": None,
                    "similarity_score": round(top.similarity, 4) if top else None,
                    "skill_id": None,
                }
            interaction_id = await self._repo.insert_interaction(
                session, workspace_id=workspace_id, user_id=auth.user_id,
                skill_id=response.get("skill_id"), query=situation,
                matched_confidence=response.get("similarity_score"),
                match_type=response["match_type"],
            )
            await session.commit()

        if response["match_type"] == "semantic":
            await cache.set_cached_search(workspace_id, situation, response)
        return {**response, "interaction_id": interaction_id, "cache_hit": False}

    async def _log_interaction(
        self,
        auth: AuthContext,
        situation: str,
        *,
        skill_id: str | None,
        confidence: float | None,
        match_type: str,
    ) -> str:
        """Log one interaction in its own tenant transaction (cache-hit path)."""
        workspace_id, role = _require_workspace(auth)
        async with get_tenant_session() as session, run_in_tenant(
            session, workspace_id, auth.user_id, role
        ):
            interaction_id = await self._repo.insert_interaction(
                session, workspace_id=workspace_id, user_id=auth.user_id,
                skill_id=skill_id, query=situation,
                matched_confidence=confidence, match_type=match_type,
            )
            await session.commit()
        return interaction_id

    async def get(self, auth: AuthContext, skill_id: str) -> SkillOut:
        """Full body of a skill.

        Agents (API keys) only ever see **published** skills — a draft or
        in-review rule isn't a promise the brain will keep, so it stays off the
        agent surface. Dashboard users (JWT) may read any status: they already see
        draft/review rows in the registry list, and the edit flow needs the full
        body of exactly those rows to prefill. 404 when unknown/soft-deleted (or,
        for an agent, when the skill isn't published)."""
        workspace_id, role = _require_workspace(auth)
        statuses = None if auth.kind == "jwt" else PUBLISHED_STATUSES
        async with get_tenant_session() as session, run_in_tenant(
            session, workspace_id, auth.user_id, role
        ):
            skill = await self._repo.get(session, skill_id, statuses=statuses)
        if skill is None:
            raise NotFoundError("Skill")
        return SkillOut(**skill)

    async def versions(self, auth: AuthContext, skill_id: str) -> list[SkillVersionOut]:
        """Version history for a published skill."""
        workspace_id, role = _require_workspace(auth)
        async with get_tenant_session() as session, run_in_tenant(
            session, workspace_id, auth.user_id, role
        ):
            skill = await self._repo.get(session, skill_id, statuses=PUBLISHED_STATUSES)
            if skill is None:
                raise NotFoundError("Skill")
            rows = await self._repo.list_versions(session, skill_id)
        return [SkillVersionOut(**r) for r in rows]

    async def export_bundle(self, auth: AuthContext) -> bytes:
        """Zip of one markdown file per published skill (the anti-lock-in export)."""
        workspace_id, role = _require_workspace(auth)
        async with get_tenant_session() as session, run_in_tenant(
            session, workspace_id, auth.user_id, role
        ):
            skills = await self._repo.list_published(session)
        # DEFLATE over every skill body is CPU-bound — run it off the event loop
        # so a large export doesn't stall every other in-flight request.
        return await asyncio.to_thread(_build_export_zip, skills)

    async def override(
        self, auth: AuthContext, interaction_id: str, reason: str | None
    ) -> OverrideResult:
        """Report an agent override (Feature 15a): drop the matched skill's
        confidence and, below the auto-publish floor, open a review.

        Idempotent per interaction — a second override on the same interaction is
        a no-op so a retrying agent can't drive confidence to zero."""
        workspace_id, role = _require_workspace(auth)
        async with get_tenant_session() as session, run_in_tenant(
            session, workspace_id, auth.user_id, role
        ):
            interaction = await self._repo.get_interaction(session, interaction_id)
            if interaction is None:
                raise NotFoundError("Interaction")
            skill_id = interaction["skill_id"]

            if interaction["human_override"]:  # already counted — don't double-decrement
                return OverrideResult(
                    interaction_id=interaction_id, skill_id=skill_id, review_created=False
                )
            await self._repo.mark_interaction_override(session, interaction_id)

            new_confidence: float | None = None
            review_created = False
            if skill_id is not None:
                new_confidence = await self._repo.decrement_confidence(
                    session, skill_id, amount=_OVERRIDE_DECREMENT
                )
                if new_confidence is not None and new_confidence < _AUTO_PUBLISH_FLOOR:
                    skill = await self._repo.get(session, skill_id)
                    if skill is not None:
                        await self._pipeline.insert_review(
                            session, workspace_id=workspace_id, title=skill["name"],
                            kind="policy_change", provider=None, source_location=None,
                            before_text=skill["base_logic"], after_text=None,
                            evidence_quote=None, evidence_author=None,
                            confidence=int(new_confidence * 100), skill_id=skill_id,
                            payload={
                                "reason": reason or "agent override reported",
                                "interactionId": interaction_id,
                            },
                        )
                        review_created = True
            await session.commit()
        if review_created:
            await cache.invalidate_skills(workspace_id)
        return OverrideResult(
            interaction_id=interaction_id, skill_id=skill_id,
            new_confidence=new_confidence, review_created=review_created,
        )


def _review_confidence(confidence: float | None) -> int:
    """Skill confidence (0–1) → the reviews table's 0–100 integer scale."""
    return max(0, min(100, round((confidence or 0.0) * 100)))


def _densify_series(counts: dict[str, int], days: int = 7) -> list[int]:
    """Turn ``{iso_date: count}`` into a fixed ``days``-length sparkline.

    Oldest first, ending today (UTC); missing days are zero-filled so every row
    returns exactly ``days`` points."""
    today = datetime.now(UTC).date()
    return [
        counts.get((today - timedelta(days=days - 1 - i)).isoformat(), 0)
        for i in range(days)
    ]


def _encode_cursor(updated_at: datetime, skill_id: str) -> str:
    """Opaque keyset cursor for ``(updated_at, id)`` pagination."""
    raw = f"{updated_at.isoformat()}|{skill_id}".encode()
    return base64.urlsafe_b64encode(raw).decode()


def _decode_cursor(cursor: str | None) -> tuple[str, str] | None:
    """Decode a cursor to ``(updated_at_iso, id)``; ``None`` for an absent/invalid one."""
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        ts, skill_id = raw.split("|", 1)
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    return ts, skill_id


def _semantic_response(skill: dict, similarity: float) -> dict:
    """Build the ``query_brain`` semantic-match payload (PRD Feature 15 schema)."""
    return {
        "skill_id": skill["id"],
        "skill_name": skill["name"],
        "trigger": skill.get("trigger"),
        "base_logic": skill.get("base_logic"),
        "exceptions": skill.get("exceptions_block") or [],
        "actions": skill.get("actions") or [],
        "confidence": skill.get("confidence"),
        "source_authority": skill.get("source_authority"),
        "version": skill.get("version"),
        "match_type": "semantic",
        "similarity_score": round(similarity, 4),
    }


# ── markdown rendering (PRD §8 Skill Format) ────────────────────────────────────

def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "skill").lower()).strip("-")
    return s or "skill"


def _build_export_zip(skills: list[dict]) -> bytes:
    """Render + DEFLATE the export bundle (sync, CPU-bound — call via to_thread)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        used: set[str] = set()
        for skill in skills:
            fname = _unique_filename(_slug(skill["name"]), used)
            zf.writestr(fname, _render_markdown(skill))
    return buf.getvalue()


def _unique_filename(slug: str, used: set[str]) -> str:
    candidate = f"{slug}.md"
    n = 2
    while candidate in used:
        candidate = f"{slug}-{n}.md"
        n += 1
    used.add(candidate)
    return candidate


def _render_markdown(skill: dict) -> str:
    lines = [f"# Skill: {skill['name']}", ""]
    lines += ["## Trigger", "", (skill.get("trigger") or "").strip(), ""]
    lines += ["## Base Logic", "", (skill.get("base_logic") or "").strip(), ""]

    lines += ["## Exceptions", ""]
    exceptions = skill.get("exceptions_block") or []
    if exceptions:
        lines += ["| Condition | Override | Source | Authority | Date |",
                  "| --- | --- | --- | --- | --- |"]
        for e in exceptions:
            lines.append(
                f"| {e.get('condition', '')} | {e.get('override') or e.get('action', '')} "
                f"| {e.get('source_url', '')} | {e.get('source_authority', '')} "
                f"| {e.get('date', '')} |"
            )
    else:
        lines.append("_None_")
    lines.append("")

    lines += ["## Actions", ""]
    actions = skill.get("actions") or []
    if actions:
        for a in actions:
            params = ", ".join(a.get("params", []) or [])
            lines.append(f"- {a.get('name', '')}({params}) → {a.get('description', '')}")
    else:
        lines.append("_None_")
    lines.append("")

    lines += ["## Source", "", f"Authority: {skill.get('source_authority') or 'unknown'}", ""]
    lines += [
        "## Metadata", "",
        f"Version: {skill.get('version')}",
        f"Confidence: {skill.get('confidence')}",
        f"Status: {skill.get('status')}",
    ]
    return "\n".join(lines) + "\n"
