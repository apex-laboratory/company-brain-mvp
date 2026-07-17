"""Reviews service + router tests (Phase 3 M5).

Service tests stub the review + skill repositories and verify the per-kind
approval mutations, the reject demotion, and the double-resolve 409. Router tests
drive the ASGI app with auth overridden, asserting the envelope and admin-only 403.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.modules.reviews import service as service_module
from app.modules.reviews.schemas import ResolveResult, ReviewOut, ReviewStats
from app.modules.reviews.service import ReviewsService
from app.pipeline.types import StageUsage
from app.shared.errors.app_error import ConflictError, NotFoundError
from app.shared.middleware.authenticate import AuthContext, get_auth_context

_NOW = datetime(2026, 7, 5, 10, 0, tzinfo=UTC)


class _AsyncCtx:
    def __init__(self, value: object) -> None:
        self._value = value

    async def __aenter__(self) -> object:
        return self._value

    async def __aexit__(self, *_: object) -> bool:
        return False


def _auth(role: str = "admin") -> AuthContext:
    return AuthContext(user_id="usr_1", workspace_id="wrk_1", role=role, scopes=[], kind="jwt")


def _review(**over) -> dict:
    base = {
        "id": "rev_1", "title": "Refund", "kind": "new_decision", "status": "pending",
        "verdict": None, "source_provider": "slack", "source_location": None,
        "before_text": None, "after_text": None, "evidence_quote": None,
        "evidence_author": None, "confidence": 80, "payload": None, "skill_id": "skl_1",
        "comment": None, "resolved_by": None, "created_at": _NOW, "resolved_at": None,
    }
    return {**base, **over}


def _skill(**over) -> dict:
    base = {
        "id": "skl_1", "workspace_id": "wrk_1", "name": "Refund", "status": "review",
        "version": "v1", "trigger": "refund asked", "base_logic": "refund within 30 days",
        "exceptions_block": [], "confidence": 0.8,
    }
    return {**base, **over}


def _svc(review: dict | None, skill: dict | None = None):
    repo = MagicMock(
        get=AsyncMock(return_value=review),
        resolve=AsyncMock(),
        list=AsyncMock(return_value=[]),
        stats=AsyncMock(return_value={"pending": 2, "approved": 6, "rejected": 2}),
    )
    skills = MagicMock(
        get_skill=AsyncMock(return_value=skill),
        insert_skill_version=AsyncMock(),
        update_skill_logic=AsyncMock(),
        apply_exception=AsyncMock(),
        set_skill_status=AsyncMock(),
    )
    svc = ReviewsService(repository=repo, skills=skills)
    session = MagicMock(commit=AsyncMock())
    patches = (
        patch.object(service_module, "get_session", return_value=_AsyncCtx(session)),
        patch.object(service_module, "run_in_tenant", return_value=_AsyncCtx(None)),
        patch.object(service_module.cache, "invalidate_skills", AsyncMock()),
        patch.object(
            service_module.embedder, "embed_text",
            AsyncMock(return_value=([0.0] * 1536, StageUsage("e", "m", 1, 0, 0.0))),
        ),
    )
    return svc, repo, skills, patches


def _enter(patches):
    for p in patches:
        p.start()


def _exit(patches):
    for p in patches:
        p.stop()


# ── approve: per kind ──────────────────────────────────────────────────────────

async def test_approve_new_decision_activates_and_versions() -> None:
    svc, repo, skills, patches = _svc(_review(kind="new_decision"), _skill(status="review"))
    _enter(patches)
    try:
        result = await svc.approve(_auth(), "rev_1", "lgtm")
    finally:
        _exit(patches)
    assert isinstance(result, ResolveResult) and result.status == "approved"
    status_call = skills.set_skill_status.await_args
    assert status_call.args[1:] == ("skl_1", "active")
    assert skills.insert_skill_version.await_args.kwargs["change_type"] == "create"
    skills.update_skill_logic.assert_not_awaited()
    repo.resolve.assert_awaited_once()


async def test_approve_policy_change_updates_logic_and_reembeds() -> None:
    review = _review(kind="policy_change", after_text="refund within 45 days")
    svc, _repo, skills, patches = _svc(review, _skill(status="active"))
    _enter(patches)
    try:
        await svc.approve(_auth(), "rev_1", None)
    finally:
        _exit(patches)
    upd = skills.update_skill_logic.await_args
    assert upd.kwargs["base_logic"] == "refund within 45 days"
    assert upd.kwargs["version"] == "v2"
    assert upd.kwargs["confidence"] == 1.0  # human-confirmed
    assert len(upd.kwargs["embedding"]) == 1536  # re-embedded on logic change


async def test_approve_exception_appends_carveout() -> None:
    review = _review(
        kind="exception",
        payload={"proposed_skill": {"exceptions": [{"condition": "gov", "action": "waive"}]}},
    )
    svc, _repo, skills, patches = _svc(review, _skill(exceptions_block=[{"condition": "vip"}]))
    _enter(patches)
    try:
        await svc.approve(_auth(), "rev_1", None)
    finally:
        _exit(patches)
    applied = skills.apply_exception.await_args.kwargs["exceptions_block"]
    assert {"condition": "vip"} in applied and {"condition": "gov", "action": "waive"} in applied
    skills.update_skill_logic.assert_not_awaited()  # base_logic untouched


async def test_approve_contradiction_applies_new_source() -> None:
    review = _review(kind="contradiction", after_text="never refund after 14 days")
    svc, _repo, skills, patches = _svc(review, _skill(status="active"))
    _enter(patches)
    try:
        await svc.approve(_auth(), "rev_1", "source B wins")
    finally:
        _exit(patches)
    assert skills.update_skill_logic.await_args.kwargs["base_logic"] == "never refund after 14 days"


# ── reject ─────────────────────────────────────────────────────────────────────

async def test_reject_new_decision_demotes_skill_to_draft() -> None:
    svc, repo, skills, patches = _svc(_review(kind="new_decision"), _skill(status="review"))
    _enter(patches)
    try:
        result = await svc.reject(_auth(), "rev_1", "not a policy")
    finally:
        _exit(patches)
    assert result.status == "rejected"
    status_call = skills.set_skill_status.await_args
    assert status_call.args[1:] == ("skl_1", "draft")
    repo.resolve.assert_awaited_once()


async def test_reject_policy_change_leaves_skill_untouched() -> None:
    svc, _repo, skills, patches = _svc(_review(kind="policy_change"), _skill(status="active"))
    _enter(patches)
    try:
        await svc.reject(_auth(), "rev_1", None)
    finally:
        _exit(patches)
    skills.set_skill_status.assert_not_awaited()  # live skill was never mutated


# ── guards ─────────────────────────────────────────────────────────────────────

async def test_resolve_already_resolved_is_conflict() -> None:
    svc, _repo, _skills, patches = _svc(_review(status="approved"))
    _enter(patches)
    try:
        with pytest.raises(ConflictError):
            await svc.approve(_auth(), "rev_1", None)
    finally:
        _exit(patches)


async def test_resolve_unknown_review_is_not_found() -> None:
    svc, _repo, _skills, patches = _svc(None)
    _enter(patches)
    try:
        with pytest.raises(NotFoundError):
            await svc.reject(_auth(), "rev_x", None)
    finally:
        _exit(patches)


async def test_stats_computes_rejection_rate() -> None:
    svc, _repo, _skills, patches = _svc(None)
    _enter(patches)
    try:
        stats = await svc.stats(_auth())
    finally:
        _exit(patches)
    assert isinstance(stats, ReviewStats)
    assert stats.rejection_rate == 0.25  # 2 / (6 + 2)


# ── router ─────────────────────────────────────────────────────────────────────

class _StubService:
    async def list(self, auth, *, status, kind, limit):
        return [ReviewOut(id="rev_1", title="Refund", kind="contradiction",
                          status="pending", created_at=_NOW)]

    async def stats(self, auth):
        return ReviewStats(pending=1, approved=3, rejected=1, rejection_rate=0.25)

    async def approve(self, auth, review_id, comment):
        return ResolveResult(id=review_id, status="approved", verdict="approve", skill_id="skl_1")

    async def reject(self, auth, review_id, comment):
        return ResolveResult(id=review_id, status="rejected", verdict="reject", skill_id="skl_1")


def _set_auth(role: str) -> None:
    from app.main import app

    app.dependency_overrides[get_auth_context] = lambda: _auth(role)


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from app.main import app
    from app.modules.reviews import router as router_module
    from app.shared.middleware.rate_limit import limiter

    limiter.enabled = False
    _set_auth("admin")
    with patch.object(router_module, "_service", _StubService()):
        transport = ASGITransport(app=app)
        try:
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                yield c
        finally:
            limiter.enabled = True
            app.dependency_overrides.clear()


async def test_list_returns_envelope(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/reviews?status=pending")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data[0]["kind"] == "contradiction"


async def test_approve_returns_result(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/reviews/rev_1/approve", json={"comment": "ok"})
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "approved"
    assert resp.json()["data"]["skillId"] == "skl_1"  # camelCase


async def test_stats_route(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/reviews/stats")
    assert resp.status_code == 200
    assert resp.json()["data"]["rejectionRate"] == 0.25


async def test_non_admin_gets_403(client: AsyncClient) -> None:
    _set_auth("editor")
    resp = await client.post("/api/v1/reviews/rev_1/reject")
    assert resp.status_code == 403
