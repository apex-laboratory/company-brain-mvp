"""Agent CRUD service + router tests (agent-builder-plan §5.3, phase 1).

Service tests inject a mocked repository and a mocked Anthropic seam, and assert
the things that would be expensive to learn in production: that the vendor call
happens **outside** the transaction, that a local-only save does not mint a
vendor version, and that a non-owner cannot edit or unpublish an agent they can
see. Router tests drive the ASGI app with auth overridden, covering the envelope
and the auth gates.

Cross-tenant isolation is enforced by RLS, which these tests cannot exercise
(they never reach Postgres). What they *can* pin is that the service always
scopes through ``run_in_tenant`` with the caller's own workspace and never a
value from the path or body — the input RLS depends on. The policy itself needs
the integration suite and a live database.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.modules.agents.schemas import AgentCreateRequest, AgentUpdateRequest
from app.modules.agents.service import AgentsService
from app.shared.errors.app_error import ForbiddenError, NotFoundError
from app.shared.middleware.authenticate import AuthContext, get_auth_context


class _AsyncCtx:
    def __init__(self, value: object) -> None:
        self._value = value

    async def __aenter__(self) -> object:
        return self._value

    async def __aexit__(self, *_: object) -> bool:
        return False


def _auth(
    kind: str = "jwt", user_id: str | None = "usr_1", role: str = "editor"
) -> AuthContext:
    return AuthContext(
        user_id=user_id,
        workspace_id="wrk_1",
        role=role,
        scopes=[],
        kind=kind,  # type: ignore[arg-type]
    )


def _row(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": "agt_1",
        "workspace_id": "wrk_1",
        "owner_user_id": "usr_1",
        "name": "Refunds",
        "description": None,
        "system_prompt": "be careful",
        "model": "claude-opus-5",
        "effort": None,
        "ground_in_brain": True,
        "budget_cents": None,
        "visibility": "private",
        "status": "draft",
        "anthropic_agent_id": "agent_remote_1",
        "anthropic_agent_version": 2,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    row.update(overrides)
    return row


def _service(repo: MagicMock, anthropic: MagicMock) -> AgentsService:
    return AgentsService(repository=repo, anthropic=anthropic)


# ── ordering: vendor call outside the transaction ─────────────────────────────


@pytest.mark.asyncio
async def test_create_calls_anthropic_before_opening_a_transaction() -> None:
    """§7: a pooled connection must never be pinned across a vendor round-trip."""
    order: list[str] = []

    repo = MagicMock()

    async def _insert(*_: Any, **__: Any) -> dict[str, Any]:
        order.append("insert")
        return _row()

    repo.insert = AsyncMock(side_effect=_insert)

    anthropic = MagicMock()

    async def _create(**__: Any) -> dict[str, Any]:
        order.append("anthropic")
        return {"id": "agent_remote_1", "version": 1}

    anthropic.create_agent = AsyncMock(side_effect=_create)

    tenant = MagicMock()
    tenant.commit = AsyncMock()

    def _open_tenant(*_: Any, **__: Any) -> Any:
        order.append("tenant_open")
        return _AsyncCtx(tenant)

    with patch(
        "app.modules.agents.service.get_tenant_session",
        return_value=_AsyncCtx(MagicMock()),
    ), patch("app.modules.agents.service.run_in_tenant", side_effect=_open_tenant):
        await _service(repo, anthropic).create_agent(
            _auth(), AgentCreateRequest(name="Refunds")
        )

    assert order.index("anthropic") < order.index("tenant_open"), order


@pytest.mark.asyncio
async def test_create_stores_the_returned_remote_id_and_version() -> None:
    repo = MagicMock()
    repo.insert = AsyncMock(return_value=_row())
    anthropic = MagicMock()
    anthropic.create_agent = AsyncMock(return_value={"id": "agent_x", "version": 7})
    tenant = MagicMock()
    tenant.commit = AsyncMock()

    with patch(
        "app.modules.agents.service.get_tenant_session",
        return_value=_AsyncCtx(MagicMock()),
    ), patch(
        "app.modules.agents.service.run_in_tenant", return_value=_AsyncCtx(tenant)
    ):
        await _service(repo, anthropic).create_agent(
            _auth(), AgentCreateRequest(name="Refunds")
        )

    stored = repo.insert.await_args.kwargs
    assert stored["anthropic_agent_id"] == "agent_x"
    assert stored["anthropic_agent_version"] == 7
    # Owner comes from the auth context, never the body — RLS WITH CHECK
    # compares it to current_user_id().
    assert stored["owner_user_id"] == "usr_1"


@pytest.mark.asyncio
async def test_create_passes_effort_inside_the_model_object() -> None:
    """Effort in a session override is silently ignored; it must ride the agent."""
    repo = MagicMock()
    repo.insert = AsyncMock(return_value=_row())
    anthropic = MagicMock()
    anthropic.create_agent = AsyncMock(return_value={"id": "a", "version": 1})
    tenant = MagicMock()
    tenant.commit = AsyncMock()

    with patch(
        "app.modules.agents.service.get_tenant_session",
        return_value=_AsyncCtx(MagicMock()),
    ), patch(
        "app.modules.agents.service.run_in_tenant", return_value=_AsyncCtx(tenant)
    ):
        await _service(repo, anthropic).create_agent(
            _auth(), AgentCreateRequest(name="R", effort="high")
        )

    assert anthropic.create_agent.await_args.kwargs["model"] == {
        "id": "claude-opus-5",
        "effort": "high",
    }


# ── local-only saves must not mint vendor versions ────────────────────────────


@pytest.mark.asyncio
async def test_local_only_update_does_not_call_anthropic() -> None:
    """Versions are the agent's audit trail — local bookkeeping must not fill it."""
    repo = MagicMock()
    repo.get = AsyncMock(return_value=_row())
    repo.update = AsyncMock(return_value=_row(ground_in_brain=False))
    anthropic = MagicMock()
    anthropic.update_agent = AsyncMock()
    tenant = MagicMock()
    tenant.commit = AsyncMock()

    with patch(
        "app.modules.agents.service.get_tenant_session",
        return_value=_AsyncCtx(MagicMock()),
    ), patch(
        "app.modules.agents.service.run_in_tenant", return_value=_AsyncCtx(tenant)
    ):
        await _service(repo, anthropic).update_agent(
            _auth(), "agt_1", AgentUpdateRequest(ground_in_brain=False)
        )

    anthropic.update_agent.assert_not_awaited()
    # The stored version is carried forward untouched.
    assert repo.update.await_args.kwargs["fields"]["anthropic_agent_version"] == 2


@pytest.mark.asyncio
async def test_runtime_update_calls_anthropic_with_the_merged_config() -> None:
    """Anthropic replaces scalars wholesale, so an unmentioned field must be resent."""
    repo = MagicMock()
    repo.get = AsyncMock(return_value=_row())
    repo.update = AsyncMock(return_value=_row(name="Refunds v2"))
    anthropic = MagicMock()
    anthropic.update_agent = AsyncMock(return_value={"id": "agent_remote_1", "version": 3})
    tenant = MagicMock()
    tenant.commit = AsyncMock()

    with patch(
        "app.modules.agents.service.get_tenant_session",
        return_value=_AsyncCtx(MagicMock()),
    ), patch(
        "app.modules.agents.service.run_in_tenant", return_value=_AsyncCtx(tenant)
    ):
        await _service(repo, anthropic).update_agent(
            _auth(), "agt_1", AgentUpdateRequest(name="Refunds v2", version=2)
        )

    sent = anthropic.update_agent.await_args.kwargs
    assert sent["name"] == "Refunds v2"
    # Not sent by the caller, but resent so Anthropic does not clear it.
    assert sent["system"] == "be careful"
    assert sent["version"] == 2
    assert repo.update.await_args.kwargs["fields"]["anthropic_agent_version"] == 3


@pytest.mark.asyncio
async def test_update_creates_remotely_when_an_earlier_sync_failed() -> None:
    """A row with no remote id must not stay permanently unrunnable."""
    repo = MagicMock()
    repo.get = AsyncMock(return_value=_row(anthropic_agent_id=None, anthropic_agent_version=None))
    repo.update = AsyncMock(return_value=_row())
    anthropic = MagicMock()
    anthropic.create_agent = AsyncMock(return_value={"id": "agent_new", "version": 1})
    anthropic.update_agent = AsyncMock()
    tenant = MagicMock()
    tenant.commit = AsyncMock()

    with patch(
        "app.modules.agents.service.get_tenant_session",
        return_value=_AsyncCtx(MagicMock()),
    ), patch(
        "app.modules.agents.service.run_in_tenant", return_value=_AsyncCtx(tenant)
    ):
        await _service(repo, anthropic).update_agent(
            _auth(), "agt_1", AgentUpdateRequest(name="Renamed")
        )

    anthropic.create_agent.assert_awaited_once()
    anthropic.update_agent.assert_not_awaited()
    assert repo.update.await_args.kwargs["fields"]["anthropic_agent_id"] == "agent_new"


# ── ownership ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_non_owner_cannot_edit_a_published_agent_they_can_see() -> None:
    """RLS grants read on a published agent; editing stays the owner's alone."""
    repo = MagicMock()
    repo.get = AsyncMock(return_value=_row(owner_user_id="usr_other", visibility="workspace"))
    anthropic = MagicMock()

    with patch(
        "app.modules.agents.service.get_tenant_session",
        return_value=_AsyncCtx(MagicMock()),
    ), patch(
        "app.modules.agents.service.run_in_tenant", return_value=_AsyncCtx(MagicMock())
    ), pytest.raises(ForbiddenError):
        await _service(repo, anthropic).update_agent(
            _auth(), "agt_1", AgentUpdateRequest(name="hijack")
        )


@pytest.mark.asyncio
async def test_invisible_agent_is_404_never_403() -> None:
    """"Exists but not yours" would leak that it exists — RLS returns no row."""
    repo = MagicMock()
    repo.get = AsyncMock(return_value=None)
    anthropic = MagicMock()

    with patch(
        "app.modules.agents.service.get_tenant_session",
        return_value=_AsyncCtx(MagicMock()),
    ), patch(
        "app.modules.agents.service.run_in_tenant", return_value=_AsyncCtx(MagicMock())
    ), pytest.raises(NotFoundError):
        await _service(repo, anthropic).get_agent(_auth(), "agt_other")


@pytest.mark.asyncio
async def test_api_key_callers_are_refused() -> None:
    """An API key has no user id, and every agent has an owner."""
    repo = MagicMock()
    with pytest.raises(ForbiddenError):
        await _service(repo, MagicMock()).list_agents(_auth(kind="api_key", user_id=None))


@pytest.mark.asyncio
async def test_tenant_scope_comes_from_auth_not_from_the_path() -> None:
    """The input RLS depends on. A path-derived workspace would defeat it."""
    repo = MagicMock()
    repo.list_visible = AsyncMock(return_value=[])
    captured: dict[str, Any] = {}

    def _run_in_tenant(_session: Any, workspace_id: str, user_id: str, role: str) -> Any:
        captured.update(workspace_id=workspace_id, user_id=user_id, role=role)
        return _AsyncCtx(MagicMock())

    with patch(
        "app.modules.agents.service.get_tenant_session",
        return_value=_AsyncCtx(MagicMock()),
    ), patch("app.modules.agents.service.run_in_tenant", side_effect=_run_in_tenant):
        await _service(repo, MagicMock()).list_agents(_auth())

    assert captured == {"workspace_id": "wrk_1", "user_id": "usr_1", "role": "editor"}


# ── versions ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_versions_are_empty_for_a_draft_that_never_synced() -> None:
    repo = MagicMock()
    repo.get = AsyncMock(return_value=_row(anthropic_agent_id=None))
    anthropic = MagicMock()
    anthropic.list_versions = AsyncMock()

    with patch(
        "app.modules.agents.service.get_tenant_session",
        return_value=_AsyncCtx(MagicMock()),
    ), patch(
        "app.modules.agents.service.run_in_tenant", return_value=_AsyncCtx(MagicMock())
    ):
        result = await _service(repo, anthropic).list_versions(_auth(), "agt_1")

    assert result == []
    anthropic.list_versions.assert_not_awaited()


@pytest.mark.asyncio
async def test_versions_check_visibility_before_proxying() -> None:
    """The proxy call carries no workspace scoping — skipping the local load
    would turn an agent id into a cross-tenant read."""
    repo = MagicMock()
    repo.get = AsyncMock(return_value=None)
    anthropic = MagicMock()
    anthropic.list_versions = AsyncMock()

    with patch(
        "app.modules.agents.service.get_tenant_session",
        return_value=_AsyncCtx(MagicMock()),
    ), patch(
        "app.modules.agents.service.run_in_tenant", return_value=_AsyncCtx(MagicMock())
    ), pytest.raises(NotFoundError):
        await _service(repo, anthropic).list_versions(_auth(), "agt_other")

    anthropic.list_versions.assert_not_awaited()


# ── router ────────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def client() -> Any:
    from app.main import app
    from app.shared.middleware.rate_limit import limiter

    limiter.enabled = False
    app.dependency_overrides[get_auth_context] = lambda: _auth()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()
    limiter.enabled = True


@pytest.mark.asyncio
async def test_list_returns_the_standard_envelope(client: AsyncClient) -> None:
    with patch(
        "app.modules.agents.router._service.list_agents", AsyncMock(return_value=[])
    ):
        response = await client.get("/api/v1/agents")

    assert response.status_code == 200
    body = response.json()
    assert body["data"] == []
    assert "requestId" in body["meta"]


@pytest.mark.asyncio
async def test_create_returns_201_and_camelcase(client: AsyncClient) -> None:
    from app.modules.agents.schemas import AgentResponse

    agent = AgentResponse(id="agt_1", name="Refunds", owner_user_id="usr_1", version=1)
    with patch(
        "app.modules.agents.router._service.create_agent",
        AsyncMock(return_value=agent),
    ):
        response = await client.post("/api/v1/agents", json={"name": "Refunds"})

    assert response.status_code == 201
    assert response.json()["data"]["groundInBrain"] is True


@pytest.mark.asyncio
async def test_unknown_body_key_is_rejected(client: AsyncClient) -> None:
    """CamelRequestModel forbids extras — mass-assignment defence."""
    response = await client.post(
        "/api/v1/agents", json={"name": "Refunds", "ownerUserId": "usr_evil"}
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_viewer_cannot_create(client: AsyncClient) -> None:
    from app.main import app

    app.dependency_overrides[get_auth_context] = lambda: _auth(role="viewer")
    response = await client.post("/api/v1/agents", json={"name": "Refunds"})
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_viewer_can_still_list(client: AsyncClient) -> None:
    """A viewer runs published agents, so reading them is not an editor action."""
    from app.main import app

    app.dependency_overrides[get_auth_context] = lambda: _auth(role="viewer")
    with patch(
        "app.modules.agents.router._service.list_agents", AsyncMock(return_value=[])
    ):
        response = await client.get("/api/v1/agents")
    assert response.status_code == 200
