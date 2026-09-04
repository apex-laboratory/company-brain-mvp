"""Agent connector tests (agent-builder-plan §5.3, phase 1).

A connector is only half a declaration until it reaches Anthropic — the row is
decorative on its own, and the agent would run without the tool its owner just
added. So most of what is worth asserting here is about the push: that it
happens, that it carries the *whole* set (both arrays are replaced wholesale),
that it happens before anything is written, and that the shapes match what the
Managed Agents API documents.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules.agents.schemas import AgentConnectorCreateRequest
from app.modules.agents.service import (
    BRAIN_CONNECTOR_NAME,
    AgentsService,
    _agent_tools,
    _mcp_servers,
)
from app.shared.errors.app_error import ConflictError, ForbiddenError, NotFoundError
from app.tests.agents.test_agents import _AsyncCtx, _auth, _row


def _connector(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": "acn_1",
        "workspace_id": "wrk_1",
        "agent_id": "agt_1",
        "name": "linear",
        "mcp_server_url": "https://mcp.linear.app/mcp",
        "provider": "linear",
        "tool_allowlist": [],
        "created_at": datetime.now(UTC),
    }
    row.update(overrides)
    return row


def _patched() -> Any:
    """The two context managers every service call opens."""
    return (
        patch(
            "app.modules.agents.service.get_tenant_session",
            return_value=_AsyncCtx(MagicMock()),
        ),
        patch(
            "app.modules.agents.service.run_in_tenant",
            return_value=_AsyncCtx(_tenant()),
        ),
    )


def _tenant() -> MagicMock:
    tenant = MagicMock()
    tenant.commit = AsyncMock()
    return tenant


# ── the config sent to Anthropic ──────────────────────────────────────────────


def test_agent_tools_always_include_the_built_in_toolset() -> None:
    """An agent with only MCP tools has no way to act on what it fetches."""
    assert _agent_tools([]) == [{"type": "agent_toolset_20260401"}]


def test_empty_allowlist_means_every_tool_not_no_tools() -> None:
    """We cannot know a pasted server's tool names before connecting, so
    defaulting to an allowlist would narrow every new connector to nothing."""
    tools = _agent_tools([_connector(tool_allowlist=[])])

    toolset = tools[1]
    assert toolset == {"type": "mcp_toolset", "mcp_server_name": "linear"}
    assert "configs" not in toolset


def test_non_empty_allowlist_becomes_default_off_plus_per_tool_configs() -> None:
    tools = _agent_tools([_connector(tool_allowlist=["search_issues"])])

    assert tools[1]["default_config"] == {"enabled": False}
    assert tools[1]["configs"] == [{"name": "search_issues", "enabled": True}]


def test_mcp_servers_carry_no_auth_field() -> None:
    """Credentials reach the server from the user's vault, matched by URL at
    session create. That split keeps secrets out of a reusable agent."""
    servers = _mcp_servers([_connector()])

    assert servers == [
        {"type": "url", "name": "linear", "url": "https://mcp.linear.app/mcp"}
    ]
    assert not any("auth" in key or "token" in key for key in servers[0])


# ── push ordering and completeness ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_add_pushes_the_whole_set_not_just_the_new_one() -> None:
    """Anthropic replaces both arrays wholesale — an incremental push would
    silently drop every connector the agent already had."""
    repo = MagicMock()
    repo.get = AsyncMock(return_value=_row())
    repo.list_connectors = AsyncMock(return_value=[_connector(name="linear")])
    repo.insert_connector = AsyncMock(return_value=_connector(id="acn_2", name="gh"))
    repo.update = AsyncMock(return_value=_row())
    anthropic = MagicMock()
    anthropic.update_agent = AsyncMock(return_value={"id": "agent_remote_1", "version": 5})

    session_patch, tenant_patch = _patched()
    with session_patch, tenant_patch:
        await AgentsService(repository=repo, anthropic=anthropic).add_connector(
            _auth(),
            "agt_1",
            AgentConnectorCreateRequest(
                name="gh", mcp_server_url="https://api.githubcopilot.com/mcp"
            ),
        )

    sent = anthropic.update_agent.await_args.kwargs
    # The Brain's own server leads, because ``_row()`` is grounded by default and
    # a grounded agent declares it on every push (service._with_grounding).
    assert [s["name"] for s in sent["mcp_servers"]] == [BRAIN_CONNECTOR_NAME, "linear", "gh"]
    assert [t.get("mcp_server_name") for t in sent["tools"][1:]] == [
        BRAIN_CONNECTOR_NAME, "linear", "gh"
    ]


@pytest.mark.asyncio
async def test_add_calls_anthropic_before_writing_anything() -> None:
    """Vendor-first, same ordering and reasoning as create_agent (§7)."""
    order: list[str] = []
    repo = MagicMock()
    repo.get = AsyncMock(return_value=_row())
    repo.list_connectors = AsyncMock(return_value=[])
    repo.update = AsyncMock(return_value=_row())

    async def _insert(*_: Any, **__: Any) -> dict[str, Any]:
        order.append("insert")
        return _connector()

    repo.insert_connector = AsyncMock(side_effect=_insert)

    anthropic = MagicMock()

    async def _update(*_: Any, **__: Any) -> dict[str, Any]:
        order.append("anthropic")
        return {"id": "agent_remote_1", "version": 5}

    anthropic.update_agent = AsyncMock(side_effect=_update)

    session_patch, tenant_patch = _patched()
    with session_patch, tenant_patch:
        await AgentsService(repository=repo, anthropic=anthropic).add_connector(
            _auth(),
            "agt_1",
            AgentConnectorCreateRequest(
                name="linear", mcp_server_url="https://mcp.linear.app/mcp"
            ),
        )

    assert order == ["anthropic", "insert"]


@pytest.mark.asyncio
async def test_remove_pushes_the_remaining_set() -> None:
    repo = MagicMock()
    repo.get = AsyncMock(return_value=_row())
    repo.list_connectors = AsyncMock(
        return_value=[_connector(id="acn_1", name="linear"), _connector(id="acn_2", name="gh")]
    )
    repo.delete_connector = AsyncMock(return_value=True)
    repo.update = AsyncMock(return_value=_row())
    anthropic = MagicMock()
    anthropic.update_agent = AsyncMock(return_value={"id": "agent_remote_1", "version": 6})

    session_patch, tenant_patch = _patched()
    with session_patch, tenant_patch:
        await AgentsService(repository=repo, anthropic=anthropic).remove_connector(
            _auth(), "agt_1", "acn_1"
        )

    sent = anthropic.update_agent.await_args.kwargs
    assert [s["name"] for s in sent["mcp_servers"]] == [BRAIN_CONNECTOR_NAME, "gh"]


@pytest.mark.asyncio
async def test_removing_the_last_connector_clears_mcp_servers() -> None:
    """Anthropic 400s on clearing mcp_servers while tools still reference one,
    so the tools array must lose its mcp_toolset entries in the same push.

    Ungrounded on purpose: a grounded agent never reaches an empty
    ``mcp_servers``, because the Brain's own server is always declared."""
    repo = MagicMock()
    repo.get = AsyncMock(return_value=_row(ground_in_brain=False))
    repo.list_connectors = AsyncMock(return_value=[_connector()])
    repo.delete_connector = AsyncMock(return_value=True)
    repo.update = AsyncMock(return_value=_row())
    anthropic = MagicMock()
    anthropic.update_agent = AsyncMock(return_value={"id": "agent_remote_1", "version": 7})

    session_patch, tenant_patch = _patched()
    with session_patch, tenant_patch:
        await AgentsService(repository=repo, anthropic=anthropic).remove_connector(
            _auth(), "agt_1", "acn_1"
        )

    sent = anthropic.update_agent.await_args.kwargs
    assert sent["mcp_servers"] == []
    assert sent["tools"] == [{"type": "agent_toolset_20260401"}]


@pytest.mark.asyncio
async def test_the_new_agent_version_is_mirrored_locally() -> None:
    repo = MagicMock()
    repo.get = AsyncMock(return_value=_row())
    repo.list_connectors = AsyncMock(return_value=[])
    repo.insert_connector = AsyncMock(return_value=_connector())
    repo.update = AsyncMock(return_value=_row())
    anthropic = MagicMock()
    anthropic.update_agent = AsyncMock(return_value={"id": "agent_remote_1", "version": 9})

    session_patch, tenant_patch = _patched()
    with session_patch, tenant_patch:
        await AgentsService(repository=repo, anthropic=anthropic).add_connector(
            _auth(),
            "agt_1",
            AgentConnectorCreateRequest(
                name="linear", mcp_server_url="https://mcp.linear.app/mcp"
            ),
        )

    assert repo.update.await_args.kwargs["fields"]["anthropic_agent_version"] == 9


# ── guards ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_duplicate_name_is_rejected_before_the_vendor_round_trip() -> None:
    """Two connectors sharing a name make mcp_server_name ambiguous."""
    repo = MagicMock()
    repo.get = AsyncMock(return_value=_row())
    repo.list_connectors = AsyncMock(return_value=[_connector(name="linear")])
    anthropic = MagicMock()
    anthropic.update_agent = AsyncMock()

    session_patch, tenant_patch = _patched()
    with session_patch, tenant_patch, pytest.raises(ConflictError):
        await AgentsService(repository=repo, anthropic=anthropic).add_connector(
            _auth(),
            "agt_1",
            AgentConnectorCreateRequest(
                name="linear", mcp_server_url="https://other.example/mcp"
            ),
        )

    anthropic.update_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_grounded_agent_reserves_one_of_the_twenty_slots() -> None:
    """§8's cap is Anthropic's, and a grounded agent spends one of it.

    Left uncounted, the twentieth user connector would save locally and then be
    rejected by the vendor, leaving the row and the agent disagreeing about what
    the agent can actually reach.
    """
    repo = MagicMock()
    repo.get = AsyncMock(return_value=_row())
    repo.list_connectors = AsyncMock(
        return_value=[_connector(id=f"acn_{i}", name=f"s{i}") for i in range(19)]
    )
    anthropic = MagicMock()
    anthropic.update_agent = AsyncMock()

    session_patch, tenant_patch = _patched()
    with session_patch, tenant_patch, pytest.raises(ConflictError, match="at most 19"):
        await AgentsService(repository=repo, anthropic=anthropic).add_connector(
            _auth(),
            "agt_1",
            AgentConnectorCreateRequest(
                name="one-too-many", mcp_server_url="https://x.example/mcp"
            ),
        )

    anthropic.update_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_ungrounded_agent_gets_all_twenty_slots() -> None:
    repo = MagicMock()
    repo.get = AsyncMock(return_value=_row(ground_in_brain=False))
    repo.list_connectors = AsyncMock(
        return_value=[_connector(id=f"acn_{i}", name=f"s{i}") for i in range(20)]
    )
    anthropic = MagicMock()
    anthropic.update_agent = AsyncMock()

    session_patch, tenant_patch = _patched()
    with session_patch, tenant_patch, pytest.raises(ConflictError, match="at most 20"):
        await AgentsService(repository=repo, anthropic=anthropic).add_connector(
            _auth(),
            "agt_1",
            AgentConnectorCreateRequest(
                name="one-too-many", mcp_server_url="https://x.example/mcp"
            ),
        )

    anthropic.update_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_owner_cannot_add_a_connector_to_a_published_agent() -> None:
    repo = MagicMock()
    repo.get = AsyncMock(
        return_value=_row(owner_user_id="usr_other", visibility="workspace")
    )
    anthropic = MagicMock()

    session_patch, tenant_patch = _patched()
    with session_patch, tenant_patch, pytest.raises(ForbiddenError):
        await AgentsService(repository=repo, anthropic=anthropic).add_connector(
            _auth(),
            "agt_1",
            AgentConnectorCreateRequest(
                name="linear", mcp_server_url="https://mcp.linear.app/mcp"
            ),
        )


@pytest.mark.asyncio
async def test_removing_an_unknown_connector_is_404_with_no_push() -> None:
    repo = MagicMock()
    repo.get = AsyncMock(return_value=_row())
    repo.list_connectors = AsyncMock(return_value=[_connector(id="acn_1")])
    anthropic = MagicMock()
    anthropic.update_agent = AsyncMock()

    session_patch, tenant_patch = _patched()
    with session_patch, tenant_patch, pytest.raises(NotFoundError):
        await AgentsService(repository=repo, anthropic=anthropic).remove_connector(
            _auth(), "agt_1", "acn_missing"
        )

    anthropic.update_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_brain_connector_name_is_reserved() -> None:
    """Two MCP servers sharing a name is a config Anthropic rejects.

    Caught here so the error names the collision, rather than arriving as a
    vendor 400 about the user's own connector — which is the one it looks like
    and the one thing that is not wrong with it.
    """
    repo = MagicMock()
    repo.get = AsyncMock(return_value=_row())
    anthropic = MagicMock()
    anthropic.update_agent = AsyncMock()

    session_patch, tenant_patch = _patched()
    with session_patch, tenant_patch, pytest.raises(ConflictError, match="reserved"):
        await AgentsService(repository=repo, anthropic=anthropic).add_connector(
            _auth(),
            "agt_1",
            AgentConnectorCreateRequest(
                name=BRAIN_CONNECTOR_NAME, mcp_server_url="https://evil.example/mcp"
            ),
        )

    anthropic.update_agent.assert_not_awaited()
