"""Skills delivery business logic (Phase 5 — Features 12/14/15a, PRD §14).

Serves the agent-facing read surface — semantic search, full body, version
history, portable export — and the override feedback loop. Vector search reuses
the tested ``PipelineRepository.similar_skills`` HNSW query; the query embedding
is computed **outside** any transaction (network I/O must never pin a pooled
connection — the orchestrator/reviews rule). Every read runs RLS-scoped.
"""
from __future__ import annotations

import io
import re
import zipfile

from app.config.database import get_tenant_session
from app.modules.skills.repository import PUBLISHED_STATUSES, SkillsRepository
from app.modules.skills.schemas import (
    OverrideResult,
    SkillOut,
    SkillSearchResult,
    SkillVersionOut,
)
from app.pipeline import cache, embedder
from app.pipeline.repository import PipelineRepository
from app.shared.errors.app_error import NotFoundError
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
        embedding, _ = await embedder.embed_text(query)
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
            await session.commit()
        return [
            SkillSearchResult(
                id=h.id, name=h.name, version=h.version, base_logic=h.base_logic,
                exceptions_block=h.exceptions_block, source_authority=h.source_authority,
                similarity=round(h.similarity, 4),
            )
            for h in hits
        ]

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

        embedding, _ = await embedder.embed_text(situation)
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
        """Full body of a published skill."""
        workspace_id, role = _require_workspace(auth)
        async with get_tenant_session() as session, run_in_tenant(
            session, workspace_id, auth.user_id, role
        ):
            skill = await self._repo.get(session, skill_id, statuses=PUBLISHED_STATUSES)
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
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            used: set[str] = set()
            for skill in skills:
                fname = _unique_filename(_slug(skill["name"]), used)
                zf.writestr(fname, _render_markdown(skill))
        return buf.getvalue()

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
