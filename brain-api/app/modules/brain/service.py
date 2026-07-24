"""Brain chat business logic (BACKEND_ASKS §7, BRAIN_CHAT_RAG_PLAN).

Phase 0 delivers the readiness gate: chat stays disabled (typed 409) until the
workspace has an embedded, reviewed skill, and a global kill-switch can hard-disable
it. Phase 1 adds ``query`` — the full RAG loop, composed from pieces that already
exist:

    gate → cache → embed → retrieve (+ provenance dossier) → synthesize → persist

Retrieval reuses ``PipelineRepository.similar_skills``; generation is the new
``synthesizer``. The two network calls (embedding, synthesis) happen OUTSIDE any
open transaction — the non-negotiable "no network I/O inside a txn" rule (CLAUDE.md
#7; pattern: reviews/service.py::approve). A miss returns an honest "no reviewed
skill covers that yet", never a fabricated answer.
"""
from __future__ import annotations

from fastapi.encoders import jsonable_encoder

from app.config.database import get_tenant_session
from app.config.settings import settings
from app.modules.brain import synthesizer
from app.modules.brain.repository import BrainRepository
from app.modules.brain.schemas import (
    BrainMessage,
    BrainProvenance,
    BrainQueryResponse,
    BrainStatusResponse,
    ConversationSummary,
    SourceCitation,
)
from app.modules.skills.repository import PUBLISHED_STATUSES, SkillsRepository
from app.pipeline import cache, embedder
from app.pipeline.repository import PipelineRepository
from app.shared.errors.app_error import (
    BrainNotReadyError,
    ForbiddenError,
    NotFoundError,
)
from app.shared.middleware.authenticate import AuthContext
from app.shared.middleware.with_tenant import run_in_tenant

_MATCH_THRESHOLD = 0.70  # below this a query is a "no_match" (mirrors SkillsService)
_RETRIEVAL_LIMIT = 5
_EVIDENCE_LIMIT = 3
# Read-cache facet: brain-chat caches a different contract than SkillsService.query()
# under the same (workspace, question) — a distinct kind keeps them from colliding
# (both stay under skills:{ws}:* so publish-time invalidation clears both).
_CACHE_KIND = "brain"
_NO_MATCH_ANSWER = (
    "I don't have a reviewed skill that covers that yet, so I can't give you a "
    "confident answer. Once a related decision has been captured and approved, "
    "I'll be able to help."
)


def _require_workspace(auth: AuthContext) -> tuple[str, str]:
    if auth.workspace_id is None or auth.role is None:
        raise RuntimeError(
            "BUG: brain service called without workspace context — "
            "ensure require_brain_access is declared on this route"
        )
    return auth.workspace_id, auth.role


def _title(question: str) -> str:
    return (question.strip().splitlines()[0] if question.strip() else "New conversation")[:60]


class BrainService:
    def __init__(
        self,
        repository: BrainRepository | None = None,
        skills: SkillsRepository | None = None,
        pipeline: PipelineRepository | None = None,
    ) -> None:
        self._repo = repository or BrainRepository()
        self._skills = skills or SkillsRepository()
        self._pipeline = pipeline or PipelineRepository()

    # ── status / gate (Phase 0) ─────────────────────────────────────────────────

    async def status(self, auth: AuthContext) -> BrainStatusResponse:
        """Readiness for the caller's workspace (drives the FE disabled state)."""
        if not settings.brain_chat_enabled:
            return BrainStatusResponse(
                enabled=False, ready=False, skills_indexed=0, reason="disabled"
            )
        workspace_id, role = _require_workspace(auth)
        async with get_tenant_session() as session, run_in_tenant(
            session, workspace_id, auth.user_id, role
        ):
            count = await self._repo.count_indexed_skills(session)
        ready = count >= 1
        return BrainStatusResponse(
            enabled=True,
            ready=ready,
            skills_indexed=count,
            reason=None if ready else "no_skills",
        )

    async def _ensure_ready(self, auth: AuthContext) -> None:
        """Gate a query: raise ``BrainNotReadyError`` if disabled or unindexed.

        Runs before embedding/synthesis so a not-ready query short-circuits with a
        typed 409 rather than spending a network call or returning a guess.
        """
        if not settings.brain_chat_enabled:
            raise BrainNotReadyError("disabled", "Brain chat is currently disabled.")
        workspace_id, role = _require_workspace(auth)
        async with get_tenant_session() as session, run_in_tenant(
            session, workspace_id, auth.user_id, role
        ):
            count = await self._repo.count_indexed_skills(session)
        if count < 1:
            raise BrainNotReadyError(
                "no_skills", "No reviewed skills have been indexed for this workspace yet."
            )

    # ── query (Phase 1) ─────────────────────────────────────────────────────────

    async def query(
        self, auth: AuthContext, question: str, conversation_id: str | None = None
    ) -> BrainQueryResponse:
        """Answer ``question`` from the workspace's reviewed skills + provenance.

        Two-phase transaction discipline: retrieve (txn A) → synthesize (no txn) →
        log + persist (txn B). A cache hit skips retrieval/synthesis entirely; a
        below-threshold or ungrounded result returns an honest no-match, uncached.
        """
        await self._ensure_ready(auth)
        workspace_id, role = _require_workspace(auth)

        cached = await cache.get_cached_search(workspace_id, question, kind=_CACHE_KIND)
        if cached is not None:
            return await self._finalize(auth, question, cached, conversation_id)

        embedding, _ = await embedder.embed_text(question)  # network — outside any txn

        # txn A: two-facet retrieval — the reviewed skills AND their evidence chunks,
        # one governed index. Full bodies + governance dossier + version history for
        # matched skills; evidence chunks for "who said / where from" questions.
        async with get_tenant_session() as session, run_in_tenant(
            session, workspace_id, auth.user_id, role
        ):
            hits = await self._pipeline.similar_skills(
                session, workspace_id, embedding, PUBLISHED_STATUSES, limit=_RETRIEVAL_LIMIT
            )
            top = hits[0] if hits else None
            skill_matched = top is not None and top.similarity >= _MATCH_THRESHOLD
            skills_ctx: list[dict] = []
            provenance: dict | None = None
            citations: dict[str, dict] = {}
            history: list[dict] = []
            if skill_matched:
                for h in hits:
                    if h.similarity < _MATCH_THRESHOLD:
                        continue
                    full = await self._skills.get(session, h.id, statuses=PUBLISHED_STATUSES)
                    if full is not None:
                        skills_ctx.append(full)
                matched_ids = [s["id"] for s in skills_ctx]
                provenance = jsonable_encoder(await self._repo.provenance(session, top.id))
                citations = await self._repo.skill_citations(session, matched_ids)
                history = await self._repo.version_history(session, matched_ids)
            evidence_hits = await self._repo.similar_chunks(
                session, workspace_id, embedding, kind="evidence", limit=_EVIDENCE_LIMIT
            )
            await session.commit()

        evidence = [e for e in evidence_hits if e["similarity"] >= _MATCH_THRESHOLD]

        # Synthesis — network I/O, strictly OUTSIDE the transaction above.
        if skill_matched and skills_ctx:
            core = await self._answer_from_skills(
                question, skills_ctx, top, provenance, history, evidence, citations
            )
        elif evidence:
            # No reviewed skill matched, but a cited source in a skill's lineage does —
            # still governed (evidence facet), never an ungoverned guess.
            core = await self._answer_from_evidence(question, evidence)
        else:
            core = _no_match_core(top.similarity if top else None)

        if core["trust"] != "none":  # cache grounded answers only
            await cache.set_cached_search(workspace_id, question, core, kind=_CACHE_KIND)
        return await self._finalize(auth, question, core, conversation_id)

    # ── history read-back (dashboard reload) ────────────────────────────────────

    async def list_conversations(
        self, auth: AuthContext, *, limit: int
    ) -> list[ConversationSummary]:
        """The signed-in user's threads, newest-active first (FE history sidebar).

        Dashboard-only: persisted conversations belong to a JWT user (RLS scopes them
        to ``current_user_id()``); an agent (API-key) never has any, so it's a 403
        rather than a misleading empty list.
        """
        workspace_id, role = self._require_dashboard(auth)
        async with get_tenant_session() as session, run_in_tenant(
            session, workspace_id, auth.user_id, role
        ):
            rows = await self._repo.list_conversations(session, limit=limit)
        return [
            ConversationSummary(
                id=r["id"], title=r["title"],
                created_at=r["created_at"], updated_at=r["updated_at"],
            )
            for r in rows
        ]

    async def list_messages(
        self, auth: AuthContext, conversation_id: str
    ) -> list[BrainMessage]:
        """Replay one thread's turns, oldest first — seeds the chat on reload.

        404s an unknown/unowned id (RLS makes another user's conversation invisible,
        which ``conversation_exists`` reports as absent)."""
        workspace_id, role = self._require_dashboard(auth)
        async with get_tenant_session() as session, run_in_tenant(
            session, workspace_id, auth.user_id, role
        ):
            if not await self._repo.conversation_exists(session, conversation_id):
                raise NotFoundError("Conversation")
            rows = await self._repo.list_messages(session, conversation_id=conversation_id)
        return [
            BrainMessage(
                id=r["id"], role=r["role"], content=r["content"],
                confidence=r["confidence"],
                sources=[_from_stored_source(s) for s in (r["sources"] or [])],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    def _require_dashboard(self, auth: AuthContext) -> tuple[str, str]:
        """Gate a dashboard-only surface: JWT callers only (RLS-owned threads)."""
        if auth.kind != "jwt":
            raise ForbiddenError("Conversation history is available to dashboard users only.")
        return _require_workspace(auth)

    async def _answer_from_skills(
        self, question: str, skills_ctx: list[dict], top, provenance: dict | None,
        history: list[dict], evidence: list[dict], citations: dict[str, dict],
    ) -> dict:
        """Skill facet: the reviewed rule is the answer; evidence supports it."""
        result = await synthesizer.answer(
            question, skills_ctx, top_similarity=top.similarity,
            provenance=provenance, history=history,
            evidence=[_evidence_ctx(e) for e in evidence],
        )
        if not result["grounded"]:
            return _no_match_core(top.similarity)
        valid_ids = {s["id"] for s in skills_ctx}
        used_ids = [i for i in result["used_skill_ids"] if i in valid_ids] or [top.id]
        return {
            "answer": result["answer"],
            "trust": "skill",
            "confidence": result["confidence"],
            "match_type": "semantic",
            "sources": _build_sources(used_ids, citations) + _evidence_sources(evidence),
            "skill_ids": used_ids,
            "provenance": provenance,
            "top_skill_id": top.id,
            "top_similarity": round(top.similarity, 4),
        }

    async def _answer_from_evidence(self, question: str, evidence: list[dict]) -> dict:
        """Evidence facet: answer from cited source material within a skill's lineage
        (governed — every evidence chunk is skill-linked), tagged trust='evidence'."""
        top_sim = evidence[0]["similarity"]
        result = await synthesizer.answer(
            question, [], top_similarity=top_sim,
            evidence=[_evidence_ctx(e) for e in evidence],
        )
        if not result["grounded"]:
            return _no_match_core(top_sim)
        skill_ids = list(dict.fromkeys(e["skill_id"] for e in evidence))
        return {
            "answer": result["answer"],
            "trust": "evidence",
            "confidence": result["confidence"],
            "match_type": "evidence",
            "sources": _evidence_sources(evidence),
            "skill_ids": skill_ids,
            "provenance": None,
            "top_skill_id": skill_ids[0] if skill_ids else None,
            "top_similarity": round(top_sim, 4),
        }

    async def _finalize(
        self, auth: AuthContext, question: str, core: dict, conversation_id: str | None
    ) -> BrainQueryResponse:
        """txn B: log the interaction and (for dashboard JWT callers) persist the
        turn. One transaction, one commit."""
        workspace_id, role = _require_workspace(auth)
        async with get_tenant_session() as session, run_in_tenant(
            session, workspace_id, auth.user_id, role
        ):
            interaction_id = await self._skills.insert_interaction(
                session, workspace_id=workspace_id, user_id=auth.user_id,
                skill_id=core.get("top_skill_id"), query=question,
                matched_confidence=core.get("top_similarity"),
                match_type=core["match_type"],
            )
            conv_id, message_id = await self._persist_turn(
                session, auth, workspace_id, question, core, conversation_id
            )
            await session.commit()
        return _build_response(
            core, interaction_id=interaction_id,
            conversation_id=conv_id, message_id=message_id,
        )

    async def _persist_turn(
        self, session, auth: AuthContext, workspace_id: str, question: str,
        core: dict, conversation_id: str | None,
    ) -> tuple[str | None, str | None]:
        """Append the user + assistant turns to the caller's conversation.

        Dashboard (JWT) only: the ``brain_conversations``/``brain_messages`` RLS
        policies are scoped to ``current_user_id()``, and a persisted thread is a
        dashboard concept — agents (API-key) get the answer + ``interactionId`` but
        no stored conversation.
        """
        if auth.kind != "jwt":
            return None, None
        if conversation_id is not None:
            if not await self._repo.conversation_exists(session, conversation_id):
                raise NotFoundError("Conversation")
            conv_id = conversation_id
        else:
            conv_id = await self._repo.create_conversation(
                session, workspace_id=workspace_id, user_id=auth.user_id,
                title=_title(question),
            )
        await self._repo.insert_message(
            session, workspace_id=workspace_id, conversation_id=conv_id,
            role="user", content=question,
        )
        stored_sources = [_to_stored_source(s) for s in core["sources"]] or None
        message_id = await self._repo.insert_message(
            session, workspace_id=workspace_id, conversation_id=conv_id,
            role="assistant", content=core["answer"],
            confidence=core["confidence"], sources=stored_sources,
        )
        await self._repo.touch_conversation(session, conv_id)
        return conv_id, message_id


def _no_match_core(top_similarity: float | None) -> dict:
    """Honest no-match: no answer synthesized, nothing cited, confidence 0."""
    return {
        "answer": _NO_MATCH_ANSWER,
        "trust": "none",
        "confidence": 0,
        "match_type": "no_match",
        "sources": [],
        "skill_ids": [],
        "provenance": None,
        "top_skill_id": None,
        "top_similarity": round(top_similarity, 4) if top_similarity is not None else None,
    }


def _build_sources(used_ids: list[str], citations: dict[str, dict]) -> list[dict]:
    """Skill-facet citations: the source document each cited skill drew on
    (``location`` = its name, ``url`` = the clickable link — e.g. the Notion page)."""
    return [
        {
            "provider": citations.get(i, {}).get("provider"),
            "location": citations.get(i, {}).get("label"),
            "skill_id": i,
            "url": citations.get(i, {}).get("url"),
            "excerpt": None,
        }
        for i in used_ids
    ]


def _evidence_ctx(hit: dict) -> dict:
    """Shape an evidence chunk for the synthesizer (content + attribution + doc)."""
    ref = hit.get("source_ref") or {}
    return {
        "content": hit.get("content"),
        "author": ref.get("author"),
        "location": ref.get("label"),
        "provider": ref.get("provider"),
        "skill_id": hit.get("skill_id"),
    }


def _evidence_sources(evidence: list[dict]) -> list[dict]:
    """Evidence-facet citations: the source document (provider + name + link) plus a
    quote from it — so the answer can point at the exact Notion page / Slack message."""
    sources = []
    for hit in evidence:
        ref = hit.get("source_ref") or {}
        content = hit.get("content") or ""
        sources.append({
            "provider": ref.get("provider"),
            "location": ref.get("label"),
            "skill_id": hit.get("skill_id"),
            "url": ref.get("url"),
            "excerpt": content[:200] or None,
        })
    return sources


def _to_stored_source(source: dict) -> dict:
    """Map an API citation to the stored ``brain_messages.sources`` shape."""
    return {
        "provider": source.get("provider"),
        "label": source.get("location"),
        "sourceItemId": source.get("skill_id"),
        "url": source.get("url"),
        "excerpt": source.get("excerpt"),
    }


def _from_stored_source(stored: dict) -> SourceCitation:
    """Inverse of ``_to_stored_source``: a persisted source → the live API citation
    shape, so a replayed answer's citations render identically to a fresh one."""
    return SourceCitation(
        provider=stored.get("provider"),
        location=stored.get("label"),
        skill_id=stored.get("sourceItemId"),
        url=stored.get("url"),
        excerpt=stored.get("excerpt"),
    )


def _build_response(
    core: dict, *, interaction_id: str, conversation_id: str | None, message_id: str | None
) -> BrainQueryResponse:
    provenance = BrainProvenance(**core["provenance"]) if core.get("provenance") else None
    return BrainQueryResponse(
        answer=core["answer"],
        trust=core["trust"],
        confidence=core["confidence"],
        match_type=core["match_type"],
        sources=[SourceCitation(**s) for s in core["sources"]],
        skill_ids=core["skill_ids"],
        provenance=provenance,
        conversation_id=conversation_id,
        message_id=message_id,
        interaction_id=interaction_id,
    )
