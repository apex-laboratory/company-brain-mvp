"""Skills delivery service + router tests (Phase 5).

Service tests stub the skills + pipeline repositories and verify search-with-
interaction-logging, published-only reads, the markdown export bundle, and the
idempotent override feedback loop. Router tests drive the ASGI app with auth
overridden, asserting the envelope, scope/role gating, and the zip response.
"""
from __future__ import annotations

import io
import zipfile
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.modules.skills import service as service_module
from app.modules.skills.schemas import OverrideResult, SkillOut, SkillVersionOut
from app.modules.skills.service import SkillsService
from app.pipeline.repository import SimilarSkill
from app.pipeline.types import StageUsage
from app.shared.errors.app_error import NotFoundError
from app.shared.middleware.authenticate import AuthContext, get_auth_context

_NOW = datetime(2026, 7, 5, 10, 0, tzinfo=UTC)


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


def _skill(**over) -> dict:
    base = {
        "id": "skl_1", "name": "Refund Handling", "version": "v3", "status": "active",
        "trigger": "customer wants refund", "base_logic": "refund within 30 days",
        "exceptions_block": [{"condition": "damaged", "override": "always approve"}],
        "actions": [{"name": "approve_refund", "params": ["order_id"], "description": "approve"}],
        "source_authority": "high", "confidence": 0.94,
        "created_at": _NOW, "updated_at": _NOW,
    }
    return {**base, **over}


def _hit(similarity: float) -> SimilarSkill:
    return SimilarSkill(
        id="skl_1", name="Refund Handling", version="v3",
        base_logic="refund within 30 days", exceptions_block=[],
        source_authority="high", similarity=similarity,
    )


def _svc(*, skill=None, hits=None, interaction=None, new_confidence=None):
    repo = MagicMock(
        get=AsyncMock(return_value=skill),
        list_versions=AsyncMock(return_value=[]),
        list_published=AsyncMock(return_value=[]),
        decrement_confidence=AsyncMock(return_value=new_confidence),
        insert_interaction=AsyncMock(return_value="int_1"),
        get_interaction=AsyncMock(return_value=interaction),
        mark_interaction_override=AsyncMock(),
    )
    pipeline = MagicMock(
        similar_skills=AsyncMock(return_value=hits or []),
        insert_review=AsyncMock(return_value="rev_1"),
    )
    svc = SkillsService(repository=repo, pipeline=pipeline)
    session = MagicMock(commit=AsyncMock())
    patches = (
        patch.object(service_module, "get_tenant_session", return_value=_AsyncCtx(session)),
        patch.object(service_module, "run_in_tenant", return_value=_AsyncCtx(None)),
        patch.object(service_module.cache, "invalidate_skills", AsyncMock()),
        patch.object(
            service_module.embedder, "embed_text",
            AsyncMock(return_value=([0.0] * 1536, StageUsage("e", "m", 1, 0, 0.0))),
        ),
    )
    return svc, repo, pipeline, patches


def _enter(patches):
    for p in patches:
        p.start()


def _exit(patches):
    for p in patches:
        p.stop()


# ── search ───────────────────────────────────────────────────────────────────

async def test_search_logs_semantic_match_above_threshold() -> None:
    svc, repo, _pipe, patches = _svc(hits=[_hit(0.91)])
    _enter(patches)
    try:
        results = await svc.search(_auth(), "refund past 30 days", 5)
    finally:
        _exit(patches)
    assert results[0].similarity == 0.91
    log = repo.insert_interaction.await_args.kwargs
    assert log["match_type"] == "semantic" and log["skill_id"] == "skl_1"


async def test_search_logs_no_match_below_threshold() -> None:
    svc, repo, _pipe, patches = _svc(hits=[_hit(0.55)])
    _enter(patches)
    try:
        await svc.search(_auth(), "unrelated", 5)
    finally:
        _exit(patches)
    log = repo.insert_interaction.await_args.kwargs
    assert log["match_type"] == "no_match" and log["skill_id"] is None


# ── query_brain core ─────────────────────────────────────────────────────────

async def test_query_semantic_returns_full_and_caches() -> None:
    svc, repo, _pipe, patches = _svc(skill=_skill(), hits=[_hit(0.91)])
    _enter(patches)
    with patch.object(service_module.cache, "get_cached_search", AsyncMock(return_value=None)), \
         patch.object(service_module.cache, "set_cached_search", AsyncMock()) as set_cache:
        try:
            result = await svc.query(_auth(), "customer wants a refund")
        finally:
            _exit(patches)
    assert result["match_type"] == "semantic"
    assert result["skill_name"] == "Refund Handling" and result["similarity_score"] == 0.91
    assert result["interaction_id"] == "int_1" and result["cache_hit"] is False
    set_cache.assert_awaited_once()  # semantic results are cached
    assert repo.insert_interaction.await_args.kwargs["match_type"] == "semantic"


async def test_query_cache_hit_skips_search_but_logs() -> None:
    svc, repo, pipe, patches = _svc()
    cached = {"match_type": "semantic", "skill_name": "Refund", "skill_id": "skl_1",
              "similarity_score": 0.9}
    _enter(patches)
    with patch.object(service_module.cache, "get_cached_search", AsyncMock(return_value=cached)):
        try:
            result = await svc.query(_auth(), "refund")
        finally:
            _exit(patches)
    assert result["cache_hit"] is True and result["interaction_id"] == "int_1"
    pipe.similar_skills.assert_not_awaited()  # served from cache, no vector search
    repo.insert_interaction.assert_awaited_once()  # still logged


async def test_query_no_match_below_threshold_not_cached() -> None:
    svc, _repo, _pipe, patches = _svc(hits=[_hit(0.4)])
    _enter(patches)
    with patch.object(service_module.cache, "get_cached_search", AsyncMock(return_value=None)), \
         patch.object(service_module.cache, "set_cached_search", AsyncMock()) as set_cache:
        try:
            result = await svc.query(_auth(), "unrelated")
        finally:
            _exit(patches)
    assert result["match_type"] == "no_match" and result["skill_id"] is None
    set_cache.assert_not_awaited()  # no_match is never cached


# ── get / versions ───────────────────────────────────────────────────────────

async def test_get_published_skill() -> None:
    svc, _repo, _pipe, patches = _svc(skill=_skill())
    _enter(patches)
    try:
        out = await svc.get(_auth(), "skl_1")
    finally:
        _exit(patches)
    assert isinstance(out, SkillOut) and out.confidence == 0.94


async def test_get_missing_is_not_found() -> None:
    svc, _repo, _pipe, patches = _svc(skill=None)
    _enter(patches)
    try:
        with pytest.raises(NotFoundError):
            await svc.get(_auth(), "skl_x")
    finally:
        _exit(patches)


async def test_versions_missing_skill_is_not_found() -> None:
    svc, _repo, _pipe, patches = _svc(skill=None)
    _enter(patches)
    try:
        with pytest.raises(NotFoundError):
            await svc.versions(_auth(), "skl_x")
    finally:
        _exit(patches)


async def test_versions_returns_history() -> None:
    svc, repo, _pipe, patches = _svc(skill=_skill())
    repo.list_versions = AsyncMock(return_value=[
        {"version": "v1", "base_logic": "old", "exceptions_block": [],
         "confidence": 0.8, "change_type": "create", "created_at": _NOW},
    ])
    _enter(patches)
    try:
        versions = await svc.versions(_auth(), "skl_1")
    finally:
        _exit(patches)
    assert len(versions) == 1 and isinstance(versions[0], SkillVersionOut)


# ── export ───────────────────────────────────────────────────────────────────

async def test_export_bundle_is_valid_zip_with_markdown() -> None:
    svc, repo, _pipe, patches = _svc()
    repo.list_published = AsyncMock(return_value=[_skill(), _skill(name="Refund Handling")])
    _enter(patches)
    try:
        data = await svc.export_bundle(_auth("admin"))
    finally:
        _exit(patches)
    zf = zipfile.ZipFile(io.BytesIO(data))
    names = zf.namelist()
    assert len(names) == 2  # duplicate names de-duped
    body = zf.read(names[0]).decode()
    assert "# Skill: Refund Handling" in body and "## Exceptions" in body


# ── override feedback loop ───────────────────────────────────────────────────

async def test_override_decrements_and_opens_review_below_floor() -> None:
    interaction = {"id": "int_1", "workspace_id": "wrk_1", "skill_id": "skl_1",
                   "human_override": False}
    svc, repo, pipe, patches = _svc(
        interaction=interaction, skill=_skill(confidence=0.88), new_confidence=0.88
    )
    _enter(patches)
    try:
        result = await svc.override(_auth(), "int_1", "wrong answer")
    finally:
        _exit(patches)
    assert isinstance(result, OverrideResult)
    assert result.new_confidence == 0.88 and result.review_created is True
    repo.decrement_confidence.assert_awaited_once()
    assert pipe.insert_review.await_args.kwargs["kind"] == "policy_change"


async def test_override_no_review_above_floor() -> None:
    interaction = {"id": "int_1", "workspace_id": "wrk_1", "skill_id": "skl_1",
                   "human_override": False}
    svc, _repo, pipe, patches = _svc(
        interaction=interaction, skill=_skill(), new_confidence=0.95
    )
    _enter(patches)
    try:
        result = await svc.override(_auth(), "int_1", None)
    finally:
        _exit(patches)
    assert result.review_created is False
    pipe.insert_review.assert_not_awaited()


async def test_override_is_idempotent() -> None:
    interaction = {"id": "int_1", "workspace_id": "wrk_1", "skill_id": "skl_1",
                   "human_override": True}
    svc, repo, _pipe, patches = _svc(interaction=interaction)
    _enter(patches)
    try:
        result = await svc.override(_auth(), "int_1", None)
    finally:
        _exit(patches)
    assert result.review_created is False
    repo.decrement_confidence.assert_not_awaited()  # already counted


async def test_override_unknown_interaction_is_not_found() -> None:
    svc, _repo, _pipe, patches = _svc(interaction=None)
    _enter(patches)
    try:
        with pytest.raises(NotFoundError):
            await svc.override(_auth(), "int_x", None)
    finally:
        _exit(patches)


# ── router ───────────────────────────────────────────────────────────────────

class _StubService:
    async def search(self, auth, query, limit):
        from app.modules.skills.schemas import SkillSearchResult
        return [SkillSearchResult(id="skl_1", name="Refund", version="v1",
                                  base_logic="x", similarity=0.9)]

    async def get(self, auth, skill_id):
        return SkillOut(id=skill_id, name="Refund", version="v1", status="active")

    async def versions(self, auth, skill_id):
        return [SkillVersionOut(version="v1")]

    async def export_bundle(self, auth):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("refund.md", "# Skill: Refund\n")
        return buf.getvalue()

    async def override(self, auth, interaction_id, reason):
        return OverrideResult(interaction_id=interaction_id, skill_id="skl_1",
                              new_confidence=0.85, review_created=True)


def _set_auth(**kw) -> None:
    from app.main import app
    app.dependency_overrides[get_auth_context] = lambda: _auth(**kw)


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from app.main import app
    from app.modules.skills import router as router_module
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


async def test_search_route_envelope(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/skills/search?q=refund")
    assert resp.status_code == 200
    assert resp.json()["data"][0]["similarity"] == 0.9


async def test_search_requires_q(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/skills/search")
    assert resp.status_code == 422


async def test_get_skill_route(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/skills/skl_1")
    assert resp.status_code == 200
    assert resp.json()["data"]["name"] == "Refund"


async def test_versions_route(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/skills/skl_1/versions")
    assert resp.status_code == 200
    assert resp.json()["data"][0]["version"] == "v1"


async def test_export_returns_zip(client: AsyncClient) -> None:
    _set_auth(role="admin")
    resp = await client.get("/api/v1/skills/export")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert zipfile.ZipFile(io.BytesIO(resp.content)).namelist() == ["refund.md"]


async def test_export_forbidden_for_viewer(client: AsyncClient) -> None:
    _set_auth(role="viewer")  # admin required
    resp = await client.get("/api/v1/skills/export")
    assert resp.status_code == 403


async def test_override_route(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/interactions/int_1/override", json={"reason": "wrong"})
    assert resp.status_code == 200
    assert resp.json()["data"]["reviewCreated"] is True


async def test_search_api_key_without_scope_forbidden(client: AsyncClient) -> None:
    _set_auth(role="viewer", kind="api_key", scopes=[])  # missing brain:query
    resp = await client.get("/api/v1/skills/search?q=refund")
    assert resp.status_code == 403


async def test_search_api_key_with_scope_ok(client: AsyncClient) -> None:
    _set_auth(role="viewer", kind="api_key", scopes=["brain:query"])
    resp = await client.get("/api/v1/skills/search?q=refund")
    assert resp.status_code == 200
