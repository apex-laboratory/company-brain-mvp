"""Brain streaming (SSE) query tests.

The streaming path must be answer-identical to the buffered one — same grounding
gate, same citations, same persistence — while emitting incrementally. The load-
bearing guarantee: a **token is never emitted for an ungrounded answer**, because the
synthesizer's schema puts ``grounded`` before ``answer``. Otherwise the client would
render a confident answer and then have it retracted.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.modules.brain import service as service_module
from app.modules.brain.service import BrainService
from app.pipeline.repository import SimilarSkill
from app.pipeline.types import StageUsage
from app.shared.middleware.authenticate import AuthContext, get_auth_context

_NOW = datetime(2026, 7, 12, 9, 0, tzinfo=UTC)


class _AsyncCtx:
    def __init__(self, value: object) -> None:
        self._value = value

    async def __aenter__(self) -> object:
        return self._value

    async def __aexit__(self, *_: object) -> bool:
        return False


def _auth(kind: str = "jwt") -> AuthContext:
    return AuthContext(
        user_id="usr_1", workspace_id="wrk_1", role="viewer",
        scopes=["brain:query"], kind=kind,  # type: ignore[arg-type]
    )


def _hit(similarity: float, sid: str = "skl_1") -> SimilarSkill:
    return SimilarSkill(
        id=sid, name="Refund Handling", version="v3",
        base_logic="refund within 45 days", exceptions_block=[],
        source_authority="high", similarity=similarity,
    )


def _full() -> dict:
    return {
        "id": "skl_1", "name": "Refund Handling", "version": "v3", "status": "active",
        "trigger": "customer wants refund", "base_logic": "refund within 45 days",
        "exceptions_block": [], "actions": [], "source_authority": "high",
        "confidence": 0.94, "created_at": _NOW, "updated_at": _NOW,
    }


def _chunks(grounded: bool, answer: str) -> list[str]:
    """The synthesizer's ordered JSON, split so `grounded` lands in the first chunk."""
    return [
        '{"grounded": ' + ("true" if grounded else "false") + ', "confidence": 0.9,',
        ' "usedSkillIds": ["skl_1"], "answer": "',
        *list(answer),
        '"}',
    ]


def _svc(*, hits, cached=None, grounded=True, answer="45 days.", evidence_hits=None):
    brain_repo = MagicMock(
        count_indexed_skills=AsyncMock(return_value=5),
        provenance=AsyncMock(return_value={"approved_by": None, "originated_by": None,
                                           "created_by": None, "last_edited_by": None}),
        skill_citations=AsyncMock(return_value={}),
        version_history=AsyncMock(return_value=[]),
        similar_chunks=AsyncMock(return_value=evidence_hits or []),
        conversation_exists=AsyncMock(return_value=True),
        create_conversation=AsyncMock(return_value="cnv_1"),
        insert_message=AsyncMock(return_value="msg_1"),
        touch_conversation=AsyncMock(),
    )
    skills_repo = MagicMock(
        get_many=AsyncMock(return_value=[_full()]),
        insert_interaction=AsyncMock(return_value="int_1"),
    )
    pipeline = MagicMock(similar_skills=AsyncMock(return_value=hits))
    svc = BrainService(repository=brain_repo, skills=skills_repo, pipeline=pipeline)

    async def _fake_stream(*_a, **_kw):
        for chunk in _chunks(grounded, answer):
            yield chunk
        yield StageUsage(stage="s", model="m", input_tokens=1, output_tokens=1, cost_usd=0.0)

    patches = (
        patch.object(service_module, "get_tenant_session",
                     return_value=_AsyncCtx(MagicMock(commit=AsyncMock()))),
        patch.object(service_module, "run_in_tenant", return_value=_AsyncCtx(None)),
        patch.object(service_module.embedder, "embed_text",
                     AsyncMock(return_value=([0.0] * 1536, StageUsage("e", "m", 1, 0, 0.0)))),
        patch.object(service_module.cache, "get_cached_search", AsyncMock(return_value=cached)),
        patch.object(service_module.cache, "set_cached_search", AsyncMock()),
        patch("app.modules.brain.synthesizer.llm_stream", _fake_stream),
    )
    return svc, brain_repo, skills_repo, patches


async def _collect(svc, auth=None) -> list[tuple[str, dict]]:
    events = await svc.start_stream(auth or _auth(), "refund window?")
    return [e async for e in events]


def _enter(patches):
    for p in patches:
        p.start()


def _exit(patches):
    for p in patches:
        p.stop()


@pytest.mark.asyncio
async def test_grounded_stream_emits_status_tokens_then_done() -> None:
    svc, _brain, _skills, patches = _svc(hits=[_hit(0.91)], answer="45 days.")
    _enter(patches)
    try:
        events = await _collect(svc)
    finally:
        _exit(patches)

    names = [n for n, _ in events]
    assert names[0] == "status" and events[0][1]["stage"] == "retrieving"
    assert "synthesizing" in [p.get("stage") for n, p in events if n == "status"]
    assert names[-1] == "done"
    tokens = "".join(p["text"] for n, p in events if n == "token")
    assert tokens == "45 days."
    done = events[-1][1]
    assert done["answer"] == "45 days."
    assert done["trust"] == "skill"
    assert done["interactionId"] == "int_1"


@pytest.mark.asyncio
async def test_ungrounded_stream_emits_no_tokens_at_all() -> None:
    # The whole point of ordering `grounded` first: never show retracted text.
    svc, _brain, skills_repo, patches = _svc(
        hits=[_hit(0.91)], grounded=False, answer="a confident but ungrounded answer"
    )
    _enter(patches)
    try:
        events = await _collect(svc)
    finally:
        _exit(patches)

    assert [n for n, _ in events if n == "token"] == []
    done = events[-1][1]
    assert done["trust"] == "none"
    assert done["confidence"] == 0
    assert "don't have a reviewed skill" in done["answer"]
    assert skills_repo.insert_interaction.await_args.kwargs["match_type"] == "no_match"


@pytest.mark.asyncio
async def test_below_threshold_streams_no_tokens_and_skips_synthesis() -> None:
    svc, _brain, _skills, patches = _svc(hits=[_hit(0.42)])
    _enter(patches)
    try:
        events = await _collect(svc)
    finally:
        _exit(patches)

    assert [n for n, _ in events if n == "token"] == []
    # No synthesis stage at all — nothing matched to synthesize from.
    assert "synthesizing" not in [p.get("stage") for n, p in events if n == "status"]
    assert events[-1][1]["trust"] == "none"


@pytest.mark.asyncio
async def test_cache_hit_streams_done_immediately_without_tokens() -> None:
    cached = {
        "answer": "cached answer", "trust": "skill", "confidence": 77,
        "match_type": "semantic", "sources": [], "skill_ids": ["skl_1"],
        "provenance": None, "top_skill_id": "skl_1", "top_similarity": 0.9,
    }
    svc, _brain, _skills, patches = _svc(hits=[_hit(0.91)], cached=cached)
    _enter(patches)
    try:
        events = await _collect(svc)
    finally:
        _exit(patches)

    assert [n for n, _ in events] == ["done"]
    assert events[0][1]["answer"] == "cached answer"


@pytest.mark.asyncio
async def test_grounded_stream_persists_the_turn() -> None:
    svc, brain_repo, skills_repo, patches = _svc(hits=[_hit(0.91)])
    _enter(patches)
    try:
        await _collect(svc)
    finally:
        _exit(patches)

    brain_repo.create_conversation.assert_awaited_once()
    assert brain_repo.insert_message.await_count == 2  # user + assistant
    skills_repo.insert_interaction.assert_awaited_once()


@pytest.mark.asyncio
async def test_stream_caches_only_grounded_answers() -> None:
    svc, _brain, _skills, patches = _svc(hits=[_hit(0.91)], grounded=False)
    _enter(patches)
    try:
        with patch.object(service_module.cache, "set_cached_search", AsyncMock()) as setter:
            await _collect(svc)
    finally:
        _exit(patches)
    setter.assert_not_awaited()


@pytest.mark.asyncio
async def test_not_ready_raises_before_stream_opens() -> None:
    from app.shared.errors.app_error import BrainNotReadyError

    svc, brain_repo, _skills, patches = _svc(hits=[_hit(0.91)])
    brain_repo.count_indexed_skills = AsyncMock(return_value=0)
    _enter(patches)
    try:
        # Must raise from start_stream itself (→ a real 409), not mid-stream.
        with pytest.raises(BrainNotReadyError):
            await svc.start_stream(_auth(), "refund window?")
    finally:
        _exit(patches)


# ── router / SSE wire format ───────────────────────────────────────────────────


class _StubStreamService:
    """Drives the route with a fixed event script (or a mid-stream failure)."""

    def __init__(self, *, raises: Exception | None = None, fail_after: int | None = None):
        self._raises = raises
        self._fail_after = fail_after

    async def start_stream(self, auth, question, conversation_id=None):
        if self._raises is not None:
            raise self._raises

        fail_after = self._fail_after

        async def gen():
            yield "status", {"stage": "retrieving"}
            yield "token", {"text": "45 "}
            if fail_after is not None:
                raise RuntimeError("synthesis blew up")
            yield "token", {"text": "days."}
            yield "done", {"answer": "45 days.", "trust": "skill", "interactionId": "int_1"}

        return gen()


def _parse_sse(body: str) -> list[tuple[str, dict]]:
    """Parse SSE frames into (event, data) pairs."""
    frames = []
    for block in body.strip().split("\n\n"):
        if not block.strip():
            continue
        event = data = None
        for line in block.splitlines():
            if line.startswith("event: "):
                event = line[len("event: "):]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: "):])
        frames.append((event, data))
    return frames


@pytest_asyncio.fixture
async def stream_client() -> AsyncIterator[AsyncClient]:
    from app.main import app
    from app.shared.middleware.rate_limit import limiter

    limiter.enabled = False
    app.dependency_overrides[get_auth_context] = lambda: _auth()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            yield c
    finally:
        limiter.enabled = True
        app.dependency_overrides.clear()


async def test_stream_route_emits_sse_frames(stream_client: AsyncClient) -> None:
    from app.modules.brain import router as router_module

    with patch.object(router_module, "_service", _StubStreamService()):
        resp = await stream_client.post(
            "/api/v1/brain/query/stream", json={"question": "refund window?"}
        )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    frames = _parse_sse(resp.text)
    assert [e for e, _ in frames] == ["status", "token", "token", "done"]
    assert "".join(d["text"] for e, d in frames if e == "token") == "45 days."
    assert frames[-1][1]["interactionId"] == "int_1"


async def test_stream_route_reports_mid_stream_failure_as_error_event(
    stream_client: AsyncClient,
) -> None:
    from app.modules.brain import router as router_module

    # The 200 is already committed once frames flush, so the failure must arrive as
    # a terminal `error` event rather than an HTTP status.
    with patch.object(router_module, "_service", _StubStreamService(fail_after=1)):
        resp = await stream_client.post(
            "/api/v1/brain/query/stream", json={"question": "refund window?"}
        )
    assert resp.status_code == 200
    frames = _parse_sse(resp.text)
    assert frames[-1][0] == "error"
    assert frames[-1][1]["code"] == "internal_error"


async def test_stream_route_409s_before_opening_when_not_ready(
    stream_client: AsyncClient,
) -> None:
    from app.modules.brain import router as router_module
    from app.shared.errors.app_error import BrainNotReadyError

    stub = _StubStreamService(raises=BrainNotReadyError("no_skills", "Nothing indexed."))
    with patch.object(router_module, "_service", stub):
        resp = await stream_client.post(
            "/api/v1/brain/query/stream", json={"question": "refund window?"}
        )
    # A real HTTP error, not a 200 carrying an error frame.
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "brain_not_ready"
