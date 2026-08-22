"""Run ingestion service + router tests (Phase 7 — PRD Feature 29).

Service tests stub the repository and assert the ingest contract: redact before
store, embed **outside** the transaction, never fail the caller on a downstream
outage. Router tests drive the ASGI app with auth overridden, asserting the
envelope, the scope gate, and the two rejection shapes a harness must be able to
tell apart (413 "send less" vs 422 "send it differently").
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.modules.runs import service as service_module
from app.modules.runs.schemas import RunAccepted, RunIngestRequest
from app.modules.runs.service import RunsService, build_digest
from app.pipeline.types import StageUsage
from app.shared.middleware.authenticate import AuthContext, get_auth_context


class _AsyncCtx:
    def __init__(self, value: object) -> None:
        self._value = value

    async def __aenter__(self) -> object:
        return self._value

    async def __aexit__(self, *_: object) -> bool:
        return False


def _auth(kind: str = "api_key", scopes: list[str] | None = None) -> AuthContext:
    return AuthContext(
        user_id=None if kind == "api_key" else "usr_1",
        workspace_id="wrk_1",
        role="viewer",
        scopes=scopes if scopes is not None else ["runs:write"],
        kind=kind,  # type: ignore[arg-type]
    )


def _body(**over: Any) -> dict[str, Any]:
    base = {
        "agentName": "support-triage-agent",
        "task": "Refund a damaged order past the 30-day window",
        "outcome": "success",
        "harness": "claude_code",
        "externalId": "cc_sess1_0",
        "steps": [
            {"index": 0, "type": "tool_call", "name": "query_brain", "status": "ok"},
            {"index": 1, "type": "file_read", "name": "policies/refunds.md", "status": "ok"},
            {"index": 2, "type": "shell", "name": "pytest -q", "status": "ok",
             "latencyMs": 8100},
            {"index": 3, "type": "file_write", "name": "app/refunds.py", "status": "ok"},
        ],
    }
    return {**base, **over}


def _svc(*, insert_result: tuple[str, bool] = ("run_1", False)):
    repo = MagicMock(insert=AsyncMock(return_value=insert_result))
    svc = RunsService(repository=repo)
    session = MagicMock(commit=AsyncMock())
    enqueue = AsyncMock()
    embed = AsyncMock(return_value=([0.1] * 1536, StageUsage("e", "m", 1, 0, 0.0)))
    patches = (
        patch.object(service_module, "get_tenant_session", return_value=_AsyncCtx(session)),
        patch.object(service_module, "run_in_tenant", return_value=_AsyncCtx(session)),
        patch.object(service_module, "enqueue", enqueue),
        patch.object(service_module.embedder, "embed_text", embed),
    )
    return svc, repo, enqueue, embed, patches


async def _ingest(svc, body: dict[str, Any], patches, auth=None) -> RunAccepted:
    for p in patches:
        p.start()
    try:
        return await svc.ingest(auth or _auth(), RunIngestRequest(**body))
    finally:
        for p in patches:
            p.stop()


# ── service ──────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_ingest_stores_redacted_trace_and_enqueues_the_gate() -> None:
    svc, repo, enqueue, _, patches = _svc()
    result = await _ingest(svc, _body(), patches)

    assert result.run_id == "run_1" and result.duplicate is False
    kwargs = repo.insert.await_args.kwargs
    assert kwargs["workspace_id"] == "wrk_1"
    assert kwargs["ingest_mode"] == "live"
    assert kwargs["eligible"] is None, "the gate judges, not ingest"
    assert len(kwargs["trace"]) == 4
    enqueue.assert_awaited_once_with("gate_run", "wrk_1", "run_1")


@pytest.mark.asyncio
async def test_secrets_in_a_step_never_reach_the_repository() -> None:
    body = _body(steps=[
        {"index": 0, "type": "shell", "name": "export TOKEN=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"},
        {"index": 1, "type": "tool_call", "name": "call", "args": {"password": "hunter2"}},
        {"index": 2, "type": "shell", "name": "pytest -q"},
    ])
    svc, repo, _, _, patches = _svc()
    await _ingest(svc, body, patches)
    stored = str(repo.insert.await_args.kwargs["trace"])
    assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345" not in stored
    assert "hunter2" not in stored


@pytest.mark.asyncio
async def test_secrets_in_the_task_string_are_scrubbed_before_embedding() -> None:
    """The task is free text a human typed; a pasted credential there also reaches
    the embedding provider, so it must be scrubbed before the call, not after."""
    svc, repo, _, embed, patches = _svc()
    await _ingest(svc, _body(task="deploy using sk-ant-api03-AAAABBBBCCCCDDDDEEEE now"), patches)
    embedded_text = embed.await_args.args[0]
    assert "sk-ant-api03-AAAABBBBCCCCDDDDEEEE" not in embedded_text
    assert "sk-ant" not in repo.insert.await_args.kwargs["task"]


@pytest.mark.asyncio
async def test_embedding_happens_before_the_transaction_opens() -> None:
    """Network I/O must never pin a pooled connection (BEST_PRACTICES §7)."""
    order: list[str] = []
    svc, repo, _, _, patches = _svc()
    repo.insert = AsyncMock(side_effect=lambda *a, **k: order.append("insert") or ("run_1", False))

    async def _embed(text: str, **_: Any):
        order.append("embed")
        return [0.1] * 1536, StageUsage("e", "m", 1, 0, 0.0)

    patches = patches[:3] + (patch.object(service_module.embedder, "embed_text", _embed),)
    await _ingest(svc, _body(), patches)
    assert order == ["embed", "insert"]


@pytest.mark.asyncio
async def test_embedding_outage_stores_the_run_unclustered_rather_than_failing() -> None:
    """The client discards its spool entry on any terminal response, so rejecting
    the push loses the run permanently. Storing it un-embedded is recoverable."""
    svc, repo, enqueue, _, patches = _svc()
    patches = patches[:3] + (
        patch.object(service_module.embedder, "embed_text",
                     AsyncMock(side_effect=RuntimeError("provider down"))),
    )
    result = await _ingest(svc, _body(), patches)
    assert result.run_id == "run_1"
    assert repo.insert.await_args.kwargs["task_embedding"] is None
    enqueue.assert_awaited_once()


@pytest.mark.asyncio
async def test_redaction_failure_drops_the_body_but_keeps_the_run() -> None:
    svc, repo, enqueue, _, patches = _svc()
    patches = patches + (
        patch.object(service_module, "redact_steps",
                     MagicMock(side_effect=service_module.RedactionFailed("boom"))),
    )
    result = await _ingest(svc, _body(), patches)

    kwargs = repo.insert.await_args.kwargs
    assert kwargs["trace"] is None, "an unredactable trace is never stored"
    assert kwargs["eligible"] is False
    assert kwargs["ineligible_reason"] == "redaction_failed"
    assert result.run_id == "run_1"
    enqueue.assert_not_awaited(), "nothing to gate — there is no trace"


@pytest.mark.asyncio
async def test_duplicate_push_is_reported_as_such() -> None:
    svc, _, _, _, patches = _svc(insert_result=("run_existing", True))
    result = await _ingest(svc, _body(), patches)
    assert result.duplicate is True and result.run_id == "run_existing"


def test_digest_summarizes_without_retaining_content() -> None:
    req = RunIngestRequest(**_body(durationMs=42_000, tokensUsed=8_100))
    steps = [s.model_dump(exclude_none=True) for s in req.steps]
    digest = build_digest(steps, req)

    assert digest["step_count"] == 4
    assert digest["tool_histogram"] == {"query_brain": 1}
    assert digest["files_touched"] == ["app/refunds.py", "policies/refunds.md"]
    assert digest["type_histogram"]["file_write"] == 1
    assert digest["duration_ms"] == 42_000 and digest["tokens_used"] == 8_100
    assert len(digest["sha256"]) == 16


# ── router ───────────────────────────────────────────────────────────────────
class _StubService:
    def __init__(self) -> None:
        self.calls: list[RunIngestRequest] = []

    async def ingest(self, auth: AuthContext, body: RunIngestRequest) -> RunAccepted:
        self.calls.append(body)
        return RunAccepted(run_id="run_1", duplicate=False)


def _set_auth(**kw) -> None:
    from app.main import app
    app.dependency_overrides[get_auth_context] = lambda: _auth(**kw)


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from app.main import app
    from app.modules.runs import router as router_module
    from app.shared.middleware.rate_limit import limiter

    limiter.enabled = False
    _set_auth()
    with patch.object(router_module, "_service", _StubService()):
        transport = ASGITransport(app=app)
        try:
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                yield c
        finally:
            limiter.enabled = True
            app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_post_runs_returns_202_receipt(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/runs", json=_body())
    assert resp.status_code == 202
    payload = resp.json()
    assert payload["data"]["runId"] == "run_1"
    assert payload["data"]["duplicate"] is False
    assert "requestId" in payload["meta"]


@pytest.mark.asyncio
async def test_missing_runs_write_scope_is_403(client: AsyncClient) -> None:
    _set_auth(scopes=["brain:query"])
    resp = await client.post("/api/v1/runs", json=_body())
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_brain_query_scope_alone_cannot_push(client: AsyncClient) -> None:
    """The write-only property in reverse: read access must not imply write access."""
    _set_auth(scopes=["brain:query", "skills:invoke", "sources:read", "decisions:read"])
    resp = await client.post("/api/v1/runs", json=_body())
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_oversize_body_is_413_not_422(client: AsyncClient) -> None:
    """A harness must be able to tell 'send less' from 'send it differently' — the
    fix for the first is splitting the trace, the fix for the second is not."""
    resp = await client.post(
        "/api/v1/runs", json=_body(), headers={"content-length": str(50 * 1024 * 1024)}
    )
    assert resp.status_code == 413
    assert "step boundary" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_out_of_order_steps_are_rejected(client: AsyncClient) -> None:
    """Compression reads the step list as a causal sequence; a shuffled trace
    distils into a procedure whose steps are in the wrong order."""
    resp = await client.post("/api/v1/runs", json=_body(steps=[
        {"index": 2, "type": "shell", "name": "pytest"},
        {"index": 0, "type": "shell", "name": "ls"},
        {"index": 1, "type": "shell", "name": "cat x"},
    ]))
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_unknown_field_is_rejected(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/runs", json=_body(bogusField="x"))
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_empty_trace_is_rejected(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/runs", json=_body(steps=[]))
    assert resp.status_code == 422
