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


def _auth(
    role: str = "viewer",
    kind: str = "jwt",
    scopes: list[str] | None = None,
    agent_origin: bool = False,
) -> AuthContext:
    return AuthContext(
        user_id="usr_1", workspace_id="wrk_1", role=role,
        scopes=scopes or [], kind=kind,  # type: ignore[arg-type]
        agent_origin=agent_origin,
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


def _hit(similarity: float, status: str = "active") -> SimilarSkill:
    return SimilarSkill(
        id="skl_1", name="Refund Handling", version="v3",
        base_logic="refund within 30 days", exceptions_block=[],
        source_authority="high", similarity=similarity, status=status,
    )


def _svc(*, skill=None, hits=None, interaction=None, new_confidence=None,
         list_rows=None, stats=None):
    repo = MagicMock(
        get=AsyncMock(return_value=skill),
        list_versions=AsyncMock(return_value=[]),
        list_published=AsyncMock(return_value=[]),
        list_skills=AsyncMock(return_value=list_rows or []),
        usage_by_ids=AsyncMock(return_value={}),
        call_series=AsyncMock(return_value={}),
        stats=AsyncMock(return_value=stats or {
            "stable": 0, "active": 0, "review": 0, "draft": 0, "total": 0, "calls30d": 0,
        }),
        insert_draft=AsyncMock(return_value="skl_new"),
        promote_draft_to_review=AsyncMock(return_value=True),
        pending_review_id=AsyncMock(return_value=None),
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


# ── agent_origin: the unreviewed path is closed ──────────────────────────────
#
# An agent that has just read a customer's Slack DM phrases its query using that
# content, so the raw query column is the one channel that can carry connector or
# file contents into the Brain unreviewed. /skills/search and query_brain both
# accept API keys, so both must store the match without the text.


async def test_search_stores_no_query_text_for_agent_origin() -> None:
    svc, repo, _pipe, patches = _svc(hits=[_hit(0.91)])
    _enter(patches)
    try:
        await svc.search(_auth(kind="api_key", agent_origin=True), "refund a VIP", 5)
    finally:
        _exit(patches)
    log = repo.insert_interaction.await_args.kwargs
    assert log["query"] is None
    # The match itself is still recorded: usage counters and the override flow
    # read these columns, not the query text.
    assert log["match_type"] == "semantic" and log["skill_id"] == "skl_1"


async def test_search_keeps_query_text_for_dashboard_jwt() -> None:
    svc, repo, _pipe, patches = _svc(hits=[_hit(0.91)])
    _enter(patches)
    try:
        await svc.search(_auth(), "refund a VIP", 5)
    finally:
        _exit(patches)
    assert repo.insert_interaction.await_args.kwargs["query"] == "refund a VIP"


async def test_query_stores_no_query_text_for_agent_origin() -> None:
    svc, repo, _pipe, patches = _svc(skill=_skill(), hits=[_hit(0.91)])
    _enter(patches)
    try:
        with patch.object(
            service_module.cache, "get_cached_search", AsyncMock(return_value=None)
        ), patch.object(service_module.cache, "set_cached_search", AsyncMock()):
            await svc.query(_auth(kind="api_key", agent_origin=True), "refund a VIP")
    finally:
        _exit(patches)
    assert repo.insert_interaction.await_args.kwargs["query"] is None


async def test_cache_hit_path_stores_no_query_text_for_agent_origin() -> None:
    """The cache-hit branch logs through a separate call site: close it too."""
    svc, repo, _pipe, patches = _svc()
    _enter(patches)
    try:
        with patch.object(
            service_module.cache, "get_cached_search",
            AsyncMock(return_value={"match_type": "semantic", "skill_id": "skl_1",
                                    "similarity_score": 0.91}),
        ):
            result = await svc.query(
                _auth(kind="api_key", agent_origin=True), "refund a VIP"
            )
    finally:
        _exit(patches)
    assert result["cache_hit"] is True
    assert repo.insert_interaction.await_args.kwargs["query"] is None


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


async def test_get_agent_is_published_only() -> None:
    # An API key (agent) may only read published skills — draft/review stay off
    # the brain surface.
    svc, repo, _pipe, patches = _svc(skill=_skill())
    _enter(patches)
    try:
        await svc.get(_auth(kind="api_key", scopes=["brain:query"]), "skl_1")
    finally:
        _exit(patches)
    assert repo.get.await_args.kwargs["statuses"] == service_module.PUBLISHED_STATUSES


async def test_get_dashboard_reads_any_status() -> None:
    # A dashboard JWT can read any status so the edit flow can prefill a draft row.
    svc, repo, _pipe, patches = _svc(skill=_skill(status="draft"))
    _enter(patches)
    try:
        out = await svc.get(_auth(role="editor"), "skl_1")
    finally:
        _exit(patches)
    assert out.status == "draft"
    assert repo.get.await_args.kwargs["statuses"] is None


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

    async def list(self, auth, *, status, source, limit, cursor):
        from app.modules.skills.schemas import SkillListItem
        return [SkillListItem(id="skl_1", name="Refund", version="v1",
                              status="active", calls30d=42, call_series=[0] * 7)], "cur_2"

    async def stats(self, auth):
        from app.modules.skills.schemas import SkillStats
        return SkillStats(total=5, stable=3, in_review=1, draft=1, calls30d=99)

    async def create(self, auth, req):
        return SkillOut(id="skl_new", name=req.name, version="v1", status="draft")

    async def update(self, auth, skill_id, req):
        return SkillOut(id=skill_id, name=req.name or "Refund", version="v2",
                        status="active")

    async def delete(self, auth, skill_id):
        return None

    async def get(self, auth, skill_id):
        return SkillOut(id=skill_id, name="Refund", version="v1", status="active")

    async def submit_for_review(self, auth, skill_id, note):
        from app.modules.skills.schemas import SubmitForReviewResult
        return SubmitForReviewResult(skill_id=skill_id, status="review",
                                     review_id="rev_1", review_created=True)

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


async def test_list_route_envelope_and_cursor(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/skills?limit=10")
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"][0]["calls30d"] == 42
    assert len(body["data"][0]["callSeries"]) == 7
    assert body["meta"]["nextCursor"] == "cur_2"


async def test_list_route_rejects_bad_status(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/skills?status=bogus")
    assert resp.status_code == 422


async def test_stats_route(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/skills/stats")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] == 5 and data["inReview"] == 1 and data["calls30d"] == 99


async def test_create_route_requires_admin(client: AsyncClient) -> None:
    _set_auth(role="editor")  # admin required
    resp = await client.post("/api/v1/skills", json={"name": "X", "baseLogic": "do X"})
    assert resp.status_code == 403


async def test_create_route_admin_ok(client: AsyncClient) -> None:
    _set_auth(role="admin")
    resp = await client.post("/api/v1/skills", json={"name": "X", "baseLogic": "do X"})
    assert resp.status_code == 201
    assert resp.json()["data"]["status"] == "draft"


async def test_update_route_editor_ok(client: AsyncClient) -> None:
    _set_auth(role="editor")
    resp = await client.patch("/api/v1/skills/skl_1", json={"name": "Renamed"})
    assert resp.status_code == 200
    assert resp.json()["data"]["name"] == "Renamed"


async def test_update_route_requires_editor(client: AsyncClient) -> None:
    _set_auth(role="viewer")  # editor required
    resp = await client.patch("/api/v1/skills/skl_1", json={"name": "X"})
    assert resp.status_code == 403


async def test_update_route_rejects_unknown_key(client: AsyncClient) -> None:
    _set_auth(role="editor")
    resp = await client.patch("/api/v1/skills/skl_1", json={"bogus": 1})
    assert resp.status_code == 422


async def test_delete_route_admin_ok(client: AsyncClient) -> None:
    _set_auth(role="admin")
    resp = await client.delete("/api/v1/skills/skl_1")
    assert resp.status_code == 200
    assert resp.json()["data"]["deleted"] is True


async def test_delete_route_requires_admin(client: AsyncClient) -> None:
    _set_auth(role="editor")  # admin required
    resp = await client.delete("/api/v1/skills/skl_1")
    assert resp.status_code == 403


async def test_submit_route_admin_ok(client: AsyncClient) -> None:
    _set_auth(role="admin")
    resp = await client.post("/api/v1/skills/skl_1/submit", json={"note": "ready"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["status"] == "review" and data["reviewId"] == "rev_1"


async def test_submit_route_without_body(client: AsyncClient) -> None:
    _set_auth(role="admin")
    resp = await client.post("/api/v1/skills/skl_1/submit")
    assert resp.status_code == 200


async def test_submit_route_requires_admin(client: AsyncClient) -> None:
    _set_auth(role="editor")  # admin required
    resp = await client.post("/api/v1/skills/skl_1/submit", json={})
    assert resp.status_code == 403


async def test_submit_route_api_key_forbidden(client: AsyncClient) -> None:
    _set_auth(role="viewer", kind="api_key", scopes=["brain:query"])  # agents can't queue
    resp = await client.post("/api/v1/skills/skl_1/submit", json={})
    assert resp.status_code == 403


async def test_submit_route_rejects_unknown_key(client: AsyncClient) -> None:
    _set_auth(role="admin")
    resp = await client.post("/api/v1/skills/skl_1/submit", json={"bogus": 1})
    assert resp.status_code == 422


async def test_create_route_rejects_unknown_key(client: AsyncClient) -> None:
    _set_auth(role="admin")
    resp = await client.post(
        "/api/v1/skills", json={"name": "X", "baseLogic": "y", "bogus": 1}
    )
    assert resp.status_code == 422


# ── list / stats / create (service) ──────────────────────────────────────────

async def test_list_densifies_series_and_returns_cursor() -> None:
    from datetime import UTC, datetime, timedelta

    today = datetime.now(UTC).date()
    row = {
        "id": "skl_1", "name": "Refund", "version": "v1", "status": "active",
        "base_logic": "x", "exceptions_block": [], "source_authority": "high",
        "source_providers": ["notion"], "description": None, "calls_30d": 7,
        "updated_at": _NOW,
    }
    svc, repo, _pipe, patches = _svc(list_rows=[row, row])  # limit+1 → has_more
    repo.call_series = AsyncMock(
        return_value={"skl_1": {today.isoformat(): 3, (today - timedelta(days=6)).isoformat(): 1}}
    )
    _enter(patches)
    try:
        items, cursor = await svc.list(_auth(), status=None, source=None, limit=1, cursor=None)
    finally:
        _exit(patches)
    assert len(items) == 1
    assert items[0].call_series == [1, 0, 0, 0, 0, 0, 3]  # oldest→newest, zero-filled
    assert cursor is not None  # a further page exists


async def test_stats_maps_buckets() -> None:
    svc, _repo, _pipe, patches = _svc(stats={
        "stable": 3, "active": 2, "review": 4, "draft": 1, "total": 10, "calls30d": 500,
    })
    _enter(patches)
    try:
        stats = await svc.stats(_auth())
    finally:
        _exit(patches)
    assert stats.stable == 5 and stats.in_review == 4 and stats.total == 10


async def test_create_embeds_then_inserts_draft_and_opens_review() -> None:
    from app.modules.skills.schemas import CreateSkillRequest

    svc, repo, pipe, patches = _svc(skill=_skill(status="draft"))
    _enter(patches)
    try:
        out = await svc.create(
            _auth(role="admin"),
            CreateSkillRequest(name="New Skill", base_logic="always X"),
        )
    finally:
        _exit(patches)
    assert out.status == "draft"
    repo.insert_draft.assert_awaited_once()
    review = pipe.insert_review.await_args.kwargs
    assert review["skill_id"] == "skl_new" and review["kind"] == "new_decision"


async def test_create_conflict_on_duplicate_name() -> None:
    from sqlalchemy.exc import IntegrityError

    from app.modules.skills.schemas import CreateSkillRequest
    from app.shared.errors.app_error import ConflictError

    svc, repo, _pipe, patches = _svc()
    repo.insert_draft = AsyncMock(side_effect=IntegrityError("dup", {}, Exception()))
    _enter(patches)
    try:
        with pytest.raises(ConflictError):
            await svc.create(
                _auth(role="admin"),
                CreateSkillRequest(name="Dup", base_logic="x"),
            )
    finally:
        _exit(patches)


# ── update / delete ──────────────────────────────────────────────────────────

async def test_update_metadata_only_skips_reembed() -> None:
    from app.modules.skills.schemas import UpdateSkillRequest

    svc, repo, _pipe, patches = _svc(skill=_skill())
    repo.update = AsyncMock(return_value=_skill(name="Renamed"))
    _enter(patches)
    try:
        out = await svc.update(
            _auth(role="editor"), "skl_1", UpdateSkillRequest(name="Renamed")
        )
    finally:
        _exit(patches)
    assert out.name == "Renamed"
    assert repo.update.await_args.kwargs["embedding"] is None  # name edit → no re-embed


async def test_update_logic_reembeds() -> None:
    from app.modules.skills.schemas import UpdateSkillRequest

    svc, repo, _pipe, patches = _svc(skill=_skill())
    repo.update = AsyncMock(return_value=_skill(base_logic="refund within 60 days"))
    _enter(patches)
    try:
        out = await svc.update(
            _auth(role="editor"), "skl_1",
            UpdateSkillRequest(base_logic="refund within 60 days"),
        )
    finally:
        _exit(patches)
    assert out.base_logic == "refund within 60 days"
    assert repo.update.await_args.kwargs["embedding"] is not None  # logic edit → re-embed


async def test_update_empty_body_returns_current_without_write() -> None:
    from app.modules.skills.schemas import UpdateSkillRequest

    svc, repo, _pipe, patches = _svc(skill=_skill())
    repo.update = AsyncMock()
    _enter(patches)
    try:
        out = await svc.update(_auth(role="editor"), "skl_1", UpdateSkillRequest())
    finally:
        _exit(patches)
    assert out.id == "skl_1"
    repo.update.assert_not_awaited()  # empty edit is a no-op


async def test_update_missing_is_not_found() -> None:
    from app.modules.skills.schemas import UpdateSkillRequest

    svc, _repo, _pipe, patches = _svc(skill=None)  # get_any 404s
    _enter(patches)
    try:
        with pytest.raises(NotFoundError):
            await svc.update(_auth(role="editor"), "skl_x", UpdateSkillRequest(name="X"))
    finally:
        _exit(patches)


async def test_update_conflict_on_duplicate_name() -> None:
    from sqlalchemy.exc import IntegrityError

    from app.modules.skills.schemas import UpdateSkillRequest
    from app.shared.errors.app_error import ConflictError

    svc, repo, _pipe, patches = _svc(skill=_skill())
    repo.update = AsyncMock(side_effect=IntegrityError("dup", {}, Exception()))
    _enter(patches)
    try:
        with pytest.raises(ConflictError):
            await svc.update(
                _auth(role="editor"), "skl_1", UpdateSkillRequest(name="Taken")
            )
    finally:
        _exit(patches)


async def test_delete_soft_deletes() -> None:
    svc, repo, _pipe, patches = _svc()
    repo.soft_delete = AsyncMock(return_value=True)
    _enter(patches)
    try:
        await svc.delete(_auth(role="admin"), "skl_1")
    finally:
        _exit(patches)
    repo.soft_delete.assert_awaited_once()


async def test_delete_missing_is_not_found() -> None:
    svc, repo, _pipe, patches = _svc()
    repo.soft_delete = AsyncMock(return_value=False)
    _enter(patches)
    try:
        with pytest.raises(NotFoundError):
            await svc.delete(_auth(role="admin"), "skl_x")
    finally:
        _exit(patches)


# ── submit a draft to the review queue ───────────────────────────────────────

async def test_submit_promotes_draft_and_opens_review() -> None:
    svc, repo, pipe, patches = _svc(skill=_skill(status="draft", confidence=0.62))
    _enter(patches)
    try:
        out = await svc.submit_for_review(_auth(role="admin"), "skl_1", "ready")
    finally:
        _exit(patches)
    assert out.status == "review" and out.review_id == "rev_1" and out.review_created
    repo.promote_draft_to_review.assert_awaited_once()
    review = pipe.insert_review.await_args.kwargs
    assert review["kind"] == "new_decision" and review["skill_id"] == "skl_1"
    assert review["after_text"] == "refund within 30 days"
    assert review["confidence"] == 62  # 0–1 skill scale → the reviews 0–100 scale
    assert review["payload"] == {"submittedBy": "usr_1", "note": "ready"}


async def test_submit_reuses_an_already_open_review() -> None:
    """A hand-authored draft already has a card — don't queue it twice."""
    svc, repo, pipe, patches = _svc(skill=_skill(status="draft"))
    repo.pending_review_id = AsyncMock(return_value="rev_existing")
    _enter(patches)
    try:
        out = await svc.submit_for_review(_auth(role="admin"), "skl_1", None)
    finally:
        _exit(patches)
    assert out.review_id == "rev_existing" and out.review_created is False
    pipe.insert_review.assert_not_awaited()


async def test_submit_missing_skill_404() -> None:
    svc, _repo, _pipe, patches = _svc(skill=None)
    _enter(patches)
    try:
        with pytest.raises(NotFoundError):
            await svc.submit_for_review(_auth(role="admin"), "skl_x", None)
    finally:
        _exit(patches)


async def test_submit_rejects_a_published_skill() -> None:
    from app.shared.errors.app_error import ConflictError

    svc, repo, pipe, patches = _svc(skill=_skill(status="active"))
    _enter(patches)
    try:
        with pytest.raises(ConflictError):
            await svc.submit_for_review(_auth(role="admin"), "skl_1", None)
    finally:
        _exit(patches)
    repo.promote_draft_to_review.assert_not_awaited()
    pipe.insert_review.assert_not_awaited()


async def test_submit_conflicts_when_another_submit_won_the_race() -> None:
    from app.shared.errors.app_error import ConflictError

    svc, repo, pipe, patches = _svc(skill=_skill(status="draft"))
    repo.promote_draft_to_review = AsyncMock(return_value=False)  # no row matched
    _enter(patches)
    try:
        with pytest.raises(ConflictError):
            await svc.submit_for_review(_auth(role="admin"), "skl_1", None)
    finally:
        _exit(patches)
    pipe.insert_review.assert_not_awaited()  # the winner owns the review row


async def test_search_result_carries_status() -> None:
    svc, _repo, _pipe, patches = _svc(hits=[_hit(0.91, status="stable")])
    _enter(patches)
    try:
        results = await svc.search(_auth(), "refund", 5)
    finally:
        _exit(patches)
    assert results[0].status == "stable"
