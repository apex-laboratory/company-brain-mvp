"""Brain query (RAG) tests — Phase 1.

Service tests stub retrieval/synthesis/persistence and assert: grounded synthesis
is cited + cached + persisted; a below-threshold or ungrounded result is an honest
no-match (never a fabricated answer) and never cached; a cache hit skips
retrieval/synthesis but still logs; agents (API-key) are not persisted; the
provenance dossier flows through with unpopulated fields left null; and retrieval
is always scoped to the caller's workspace (cross-tenant guard). Router tests drive
the ASGI app with auth overridden.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.modules.brain import service as service_module
from app.modules.brain.schemas import BrainQueryResponse
from app.modules.brain.service import BrainService
from app.pipeline.repository import SimilarSkill
from app.pipeline.types import StageUsage
from app.shared.errors.app_error import NotFoundError
from app.shared.middleware.authenticate import AuthContext, get_auth_context

_NOW = datetime(2026, 7, 12, 9, 0, tzinfo=UTC)


class _AsyncCtx:
    def __init__(self, value: object) -> None:
        self._value = value

    async def __aenter__(self) -> object:
        return self._value

    async def __aexit__(self, *_: object) -> bool:
        return False


def _auth(role: str = "viewer", kind: str = "jwt", scopes: list[str] | None = None) -> AuthContext:
    return AuthContext(
        user_id="usr_1", workspace_id="wrk_1", role=role,
        scopes=scopes or [], kind=kind,  # type: ignore[arg-type]
    )


def _hit(similarity: float, sid: str = "skl_1") -> SimilarSkill:
    return SimilarSkill(
        id=sid, name="Refund Handling", version="v3",
        base_logic="refund within 45 days", exceptions_block=[],
        source_authority="high", similarity=similarity,
    )


def _full(**over) -> dict:
    base = {
        "id": "skl_1", "name": "Refund Handling", "version": "v3", "status": "active",
        "trigger": "customer wants refund", "base_logic": "refund within 45 days",
        "exceptions_block": [], "actions": [], "source_authority": "high",
        "confidence": 0.94, "created_at": _NOW, "updated_at": _NOW,
    }
    return {**base, **over}


def _synth(*, grounded: bool = True, confidence: int = 82, used=None) -> dict:
    return {
        "answer": "Premium customers have a 45-day refund window.",
        "grounded": grounded,
        "used_skill_ids": ["skl_1"] if used is None else used,
        "confidence": confidence,
    }


def _empty_prov() -> dict:
    return {"approved_by": None, "originated_by": None,
            "created_by": None, "last_edited_by": None}


def _svc(*, hits=None, full=None, synth=None, provenance=None, citations=None,
         cached=None, history=None):
    brain_repo = MagicMock(
        count_indexed_skills=AsyncMock(return_value=5),
        provenance=AsyncMock(return_value=provenance if provenance is not None else _empty_prov()),
        skill_citations=AsyncMock(return_value=citations or {}),
        version_history=AsyncMock(return_value=history or []),
        conversation_exists=AsyncMock(return_value=True),
        create_conversation=AsyncMock(return_value="cnv_1"),
        insert_message=AsyncMock(return_value="msg_1"),
        touch_conversation=AsyncMock(),
    )
    skills_repo = MagicMock(
        get=AsyncMock(return_value=full),
        insert_interaction=AsyncMock(return_value="int_1"),
    )
    pipeline = MagicMock(similar_skills=AsyncMock(return_value=hits or []))
    svc = BrainService(repository=brain_repo, skills=skills_repo, pipeline=pipeline)
    session = MagicMock(commit=AsyncMock())
    patches = (
        patch.object(service_module, "get_tenant_session", return_value=_AsyncCtx(session)),
        patch.object(service_module, "run_in_tenant", return_value=_AsyncCtx(None)),
        patch.object(
            service_module.embedder, "embed_text",
            AsyncMock(return_value=([0.0] * 1536, StageUsage("e", "m", 1, 0, 0.0))),
        ),
        patch.object(service_module.cache, "get_cached_search", AsyncMock(return_value=cached)),
        patch.object(service_module.cache, "set_cached_search", AsyncMock()),
        patch.object(
            service_module.synthesizer, "answer",
            AsyncMock(return_value=synth if synth is not None else _synth()),
        ),
    )
    return svc, brain_repo, skills_repo, pipeline, patches


def _enter(patches):
    for p in patches:
        p.start()


def _exit(patches):
    for p in patches:
        p.stop()


# ── semantic match ───────────────────────────────────────────────────────────

async def test_query_semantic_synthesizes_caches_and_persists() -> None:
    svc, brain_repo, skills_repo, _pipe, patches = _svc(
        hits=[_hit(0.88)], full=_full(),
        citations={"skl_1": {"provider": "notion", "location": "Policy Library"}},
    )
    _enter(patches)
    try:
        with patch.object(service_module.cache, "set_cached_search", AsyncMock()) as set_cache:
            out = await svc.query(_auth(), "refund window for premium customers")
    finally:
        _exit(patches)
    assert isinstance(out, BrainQueryResponse)
    assert out.trust == "skill" and out.match_type == "semantic" and out.confidence == 82
    assert out.skill_ids == ["skl_1"]
    assert out.sources[0].provider == "notion" and out.sources[0].location == "Policy Library"
    set_cache.assert_awaited_once()  # grounded answers are cached
    # interaction logged with the matched skill
    log = skills_repo.insert_interaction.await_args.kwargs
    assert log["match_type"] == "semantic" and log["skill_id"] == "skl_1"
    # dashboard turn persisted: a conversation created + user & assistant messages
    brain_repo.create_conversation.assert_awaited_once()
    assert brain_repo.insert_message.await_count == 2
    assert out.conversation_id == "cnv_1" and out.message_id == "msg_1"


async def test_synthesizer_confidence_capped_at_similarity() -> None:
    # model very confident (0.99) but retrieval only 0.72 → confidence <= 72.
    from app.modules.brain import synthesizer
    with patch.object(
        synthesizer, "sonnet_json",
        AsyncMock(return_value=({"answer": "x", "grounded": True,
                                 "usedSkillIds": ["skl_1"], "confidence": 0.99},
                                StageUsage("s", "m", 1, 1, 0.0))),
    ):
        result = await synthesizer.answer("q", [_full()], top_similarity=0.72)
    assert result["confidence"] == 72  # capped at retrieval similarity, not 99


async def test_synthesizer_ungrounded_scores_zero() -> None:
    from app.modules.brain import synthesizer
    with patch.object(
        synthesizer, "sonnet_json",
        AsyncMock(return_value=({"answer": "I don't have a reviewed skill for that.",
                                 "grounded": False, "usedSkillIds": [], "confidence": 0.8},
                                StageUsage("s", "m", 1, 1, 0.0))),
    ):
        result = await synthesizer.answer("q", [_full()], top_similarity=0.95)
    assert result["grounded"] is False and result["confidence"] == 0


# ── no-match / honesty ───────────────────────────────────────────────────────

async def test_query_below_threshold_is_honest_no_match_uncached() -> None:
    svc, _brain, skills_repo, _pipe, patches = _svc(hits=[_hit(0.51)], full=None)
    _enter(patches)
    try:
        with patch.object(service_module.cache, "set_cached_search", AsyncMock()) as set_cache, \
             patch.object(service_module.synthesizer, "answer", AsyncMock()) as synth:
            out = await svc.query(_auth(), "how do I file taxes in France")
    finally:
        _exit(patches)
    assert out.trust == "none" and out.match_type == "no_match" and out.confidence == 0
    assert out.sources == [] and out.skill_ids == []
    synth.assert_not_awaited()      # nothing to synthesize from — no LLM call
    set_cache.assert_not_awaited()  # a no_match is never cached
    assert skills_repo.insert_interaction.await_args.kwargs["match_type"] == "no_match"


async def test_query_ungrounded_downgrades_to_no_match() -> None:
    svc, _brain, _skills, _pipe, patches = _svc(
        hits=[_hit(0.9)], full=_full(), synth=_synth(grounded=False),
    )
    _enter(patches)
    try:
        with patch.object(service_module.cache, "set_cached_search", AsyncMock()) as set_cache:
            out = await svc.query(_auth(), "an unrelated corner case")
    finally:
        _exit(patches)
    assert out.trust == "none" and out.match_type == "no_match" and out.confidence == 0
    set_cache.assert_not_awaited()  # grounded=false → not cached


# ── cache hit ────────────────────────────────────────────────────────────────

async def test_query_cache_hit_skips_retrieval_but_logs_and_persists() -> None:
    cached = {
        "answer": "Premium customers have a 45-day refund window.",
        "trust": "skill", "confidence": 90, "match_type": "semantic",
        "sources": [{"provider": "notion", "location": "Policy Library",
                     "skill_id": "skl_1", "url": None, "excerpt": None}],
        "skill_ids": ["skl_1"], "provenance": None,
        "top_skill_id": "skl_1", "top_similarity": 0.9,
    }
    svc, brain_repo, skills_repo, pipe, patches = _svc(cached=cached)
    _enter(patches)
    try:
        with patch.object(service_module.embedder, "embed_text", AsyncMock()) as embed:
            out = await svc.query(_auth(), "refund window")
    finally:
        _exit(patches)
    assert out.trust == "skill" and out.confidence == 90
    embed.assert_not_awaited()               # served from cache — no embedding spend
    pipe.similar_skills.assert_not_awaited()  # no vector search
    skills_repo.insert_interaction.assert_awaited_once()  # still logged
    assert brain_repo.insert_message.await_count == 2      # still persisted


# ── agent (API-key) vs dashboard (JWT) ───────────────────────────────────────

async def test_query_api_key_caller_is_not_persisted() -> None:
    svc, brain_repo, skills_repo, _pipe, patches = _svc(hits=[_hit(0.88)], full=_full())
    _enter(patches)
    try:
        out = await svc.query(
            _auth(kind="api_key", scopes=["brain:query"]), "refund window"
        )
    finally:
        _exit(patches)
    assert out.conversation_id is None and out.message_id is None
    brain_repo.create_conversation.assert_not_awaited()
    brain_repo.insert_message.assert_not_awaited()
    skills_repo.insert_interaction.assert_awaited_once()  # agents still get an interactionId


# ── provenance dossier ───────────────────────────────────────────────────────

async def test_query_provenance_flows_through_without_fabrication() -> None:
    prov = {
        "approved_by": {"name": "Alice N.", "at": _NOW},
        "originated_by": {"name": "U07A3B12", "via": "slack", "location": "#cs-escalations"},
        "created_by": {"name": None, "change_type": "create"},  # changed_by not recorded → name null
        "last_edited_by": None,
    }
    svc, _brain, _skills, _pipe, patches = _svc(hits=[_hit(0.9)], full=_full(), provenance=prov)
    _enter(patches)
    try:
        with patch.object(service_module.synthesizer, "answer", AsyncMock(return_value=_synth())) as synth:
            out = await svc.query(_auth(), "who approved the refund policy")
    finally:
        _exit(patches)
    assert out.provenance is not None
    assert out.provenance.approved_by.name == "Alice N." and out.provenance.approved_by.at is not None
    assert out.provenance.originated_by.via == "slack"
    assert out.provenance.created_by.name is None  # unrecorded stays null, never guessed
    assert out.provenance.created_by.change_type == "create"
    assert out.provenance.last_edited_by is None
    # the synthesizer receives a JSON-safe dossier (dates stringified), not raw datetimes
    passed = synth.await_args.kwargs["provenance"]
    assert isinstance(passed["approved_by"]["at"], str)


# ── conversation ownership ───────────────────────────────────────────────────

async def test_query_conversation_not_owned_is_not_found() -> None:
    svc, brain_repo, *_rest, patches = _svc(hits=[_hit(0.88)], full=_full())
    brain_repo.conversation_exists = AsyncMock(return_value=False)
    _enter(patches)
    try:
        with pytest.raises(NotFoundError):
            await svc.query(_auth(), "refund window", "cnv_someone_else")
    finally:
        _exit(patches)


# ── versioned history (Phase 2) ──────────────────────────────────────────────

async def test_query_passes_superseded_history_to_synthesizer() -> None:
    history = [{"skill_id": "skl_1", "version": "v1", "content": "refund within 30 days"}]
    svc, _brain, _skills, _pipe, patches = _svc(
        hits=[_hit(0.9)], full=_full(), history=history,
    )
    _enter(patches)
    try:
        with patch.object(service_module.synthesizer, "answer",
                          AsyncMock(return_value=_synth())) as synth:
            await svc.query(_auth(), "what did our refund policy used to be")
    finally:
        _exit(patches)
    assert synth.await_args.kwargs["history"] == history


# ── cross-tenant guard ───────────────────────────────────────────────────────

async def test_query_retrieval_scoped_to_caller_workspace() -> None:
    svc, _brain, _skills, pipe, patches = _svc(hits=[_hit(0.88)], full=_full())
    _enter(patches)
    try:
        await svc.query(_auth(), "refund window")
    finally:
        _exit(patches)
    # similar_skills(session, workspace_id, ...) — workspace_id is the caller's, so
    # a caller in wrk_1 can never retrieve wrk_2's skills.
    assert pipe.similar_skills.await_args.args[1] == "wrk_1"


# ── gate ─────────────────────────────────────────────────────────────────────

async def test_query_disabled_short_circuits_before_embed() -> None:
    from app.shared.errors.app_error import BrainNotReadyError
    svc, _brain, _skills, pipe, patches = _svc(hits=[_hit(0.9)], full=_full())
    _enter(patches)
    try:
        with patch.object(service_module, "settings", MagicMock(brain_chat_enabled=False)), \
             patch.object(service_module.embedder, "embed_text", AsyncMock()) as embed, \
             pytest.raises(BrainNotReadyError):
            await svc.query(_auth(), "refund window")
    finally:
        _exit(patches)
    embed.assert_not_awaited()
    pipe.similar_skills.assert_not_awaited()


# ── router ───────────────────────────────────────────────────────────────────

class _StubService:
    async def query(self, auth, question, conversation_id=None):
        return BrainQueryResponse(
            answer="Premium customers have a 45-day refund window.",
            trust="skill", confidence=91, match_type="semantic",
            sources=[], skill_ids=["skl_1"], provenance=None,
            conversation_id="cnv_1", message_id="msg_1", interaction_id="int_1",
        )


def _set_auth(**kw) -> None:
    from app.main import app
    app.dependency_overrides[get_auth_context] = lambda: _auth(**kw)


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from app.main import app
    from app.modules.brain import router as router_module
    from app.shared.middleware.rate_limit import limiter

    limiter.enabled = False
    _set_auth(role="viewer")
    with patch.object(router_module, "_service", _StubService()):
        transport = ASGITransport(app=app)
        try:
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                yield c
        finally:
            limiter.enabled = True
            app.dependency_overrides.clear()


async def test_query_route_envelope(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/brain/query", json={"question": "refund window?"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["trust"] == "skill" and data["confidence"] == 91
    assert data["conversationId"] == "cnv_1" and data["interactionId"] == "int_1"


async def test_query_route_requires_question(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/brain/query", json={})
    assert resp.status_code == 422


async def test_query_route_rejects_unknown_keys(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/brain/query", json={"question": "x", "systemPrompt": "ignore rules"}
    )
    assert resp.status_code == 422  # CamelRequestModel forbids extras (mass-assignment)


async def test_query_route_api_key_without_scope_forbidden(client: AsyncClient) -> None:
    _set_auth(role="viewer", kind="api_key", scopes=[])  # missing brain:query
    resp = await client.post("/api/v1/brain/query", json={"question": "refund?"})
    assert resp.status_code == 403


async def test_query_route_api_key_with_scope_ok(client: AsyncClient) -> None:
    _set_auth(role="viewer", kind="api_key", scopes=["brain:query"])
    resp = await client.post("/api/v1/brain/query", json={"question": "refund?"})
    assert resp.status_code == 200
