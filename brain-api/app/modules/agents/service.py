"""Agent-builder business logic (agent-builder-plan §5.3, phase 1).

Two constraints shape every method here.

**Network I/O never happens inside a transaction.** Every Anthropic call is made
before the tenant session opens, or between two of them — never with one held.
An agent create is a multi-second round-trip, and a pooled connection pinned for
its duration is precisely the failure the two-phase pattern in
``reviews/service.py::approve`` exists to prevent (``BACKEND_BEST_PRACTICES.md``
§7). Where that forces a read-then-call-then-write shape, it is written out
rather than hidden.

**Anthropic is the source of truth for the agent object; we mirror it.** The
local row carries ``anthropic_agent_id`` and ``anthropic_agent_version`` so a
session can pin a version, but the history itself is proxied on demand, never
copied. A mirror that is one failed write away from disagreeing with the vendor
is worse than no mirror.

The ordering that follows from those two — **vendor first, then store** — has one
visible consequence worth naming: if the vendor call succeeds and the insert then
fails, an Anthropic agent exists that no row references. That is logged and left
(agents have no delete, and archiving on a failed insert would make a transient
DB blip permanently destroy the object). The reverse ordering would be worse: a
local row claiming a remote agent that was never created reads as success to the
user and fails at session start, much later and much further from the cause.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.exc import IntegrityError

from app.config.database import get_tenant_session
from app.modules.agents.anthropic_client import AnthropicAgentsClient
from app.modules.agents.repository import AgentsRepository
from app.modules.agents.schemas import (
    MAX_CONNECTORS_PER_AGENT,
    AgentConnectorCreateRequest,
    AgentConnectorResponse,
    AgentCreateRequest,
    AgentResponse,
    AgentUpdateRequest,
    AgentVersionResponse,
)
from app.shared.errors.app_error import ConflictError, ForbiddenError, NotFoundError
from app.shared.helpers.ids import generate_id
from app.shared.middleware.authenticate import AuthContext
from app.shared.middleware.with_tenant import run_in_tenant

log = logging.getLogger(__name__)


def _require_dashboard_user(auth: AuthContext) -> tuple[str, str, str]:
    """Agent building is a dashboard action — return ``(workspace, user, role)``.

    An API key resolves to a workspace but not to a person, and every agent has
    an owner: ``agent_definitions``' RLS policy compares ``owner_user_id`` to
    ``current_user_id()``, so an ownerless caller could not read back what it
    wrote. Failing here makes that a clear 403 instead of a row that vanishes.
    """
    if auth.workspace_id is None or auth.role is None:
        raise ForbiddenError("This action needs a workspace.")
    if auth.kind != "jwt" or not auth.user_id:
        raise ForbiddenError("Agents can only be managed by a signed-in user.")
    return auth.workspace_id, auth.user_id, auth.role


def _to_response(row: dict[str, Any], *, caller_user_id: str) -> AgentResponse:
    return AgentResponse(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        system_prompt=row["system_prompt"],
        model=row["model"],
        effort=row["effort"],
        ground_in_brain=row["ground_in_brain"],
        budget_cents=row["budget_cents"],
        visibility=row["visibility"],
        status=row["status"],
        owner_user_id=row["owner_user_id"],
        is_owner=row["owner_user_id"] == caller_user_id,
        version=row["anthropic_agent_version"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _model_config(model: str | None, effort: str | None) -> str | dict[str, Any]:
    """Anthropic's ``model`` field: a bare string, or an object carrying effort.

    Effort is agent configuration only — Anthropic silently ignores an effort set
    in a per-session override — so it has to ride the agent's model object or it
    has no effect at all.
    """
    if effort:
        return {"id": model, "effort": effort}
    return model or "claude-opus-5"


class AgentsService:
    def __init__(
        self,
        repository: AgentsRepository | None = None,
        anthropic: AnthropicAgentsClient | None = None,
    ) -> None:
        self._repo = repository or AgentsRepository()
        self._anthropic = anthropic or AnthropicAgentsClient()

    # ── reads ─────────────────────────────────────────────────────────────────

    async def list_agents(self, auth: AuthContext) -> list[AgentResponse]:
        """Every agent visible to the caller: own private plus workspace-published."""
        workspace_id, user_id, role = _require_dashboard_user(auth)
        async with get_tenant_session() as session, run_in_tenant(
            session, workspace_id, user_id, role
        ) as tenant:
            rows = await self._repo.list_visible(tenant, workspace_id=workspace_id)
        return [_to_response(row, caller_user_id=user_id) for row in rows]

    async def get_agent(self, auth: AuthContext, agent_id: str) -> AgentResponse:
        workspace_id, user_id, role = _require_dashboard_user(auth)
        row = await self._load(workspace_id, user_id, role, agent_id)
        return _to_response(row, caller_user_id=user_id)

    async def list_versions(
        self, auth: AuthContext, agent_id: str
    ) -> list[AgentVersionResponse]:
        """Proxy the agent's version history from Anthropic.

        The local row is loaded first only to prove the caller may see this agent
        — the proxy call itself carries no workspace scoping, so skipping that
        check would turn an agent id into a cross-tenant read.
        """
        workspace_id, user_id, role = _require_dashboard_user(auth)
        row = await self._load(workspace_id, user_id, role, agent_id)
        remote_id = row["anthropic_agent_id"]
        if not remote_id:
            # A draft that has never synced has no history yet — an empty list,
            # not an error: the builder renders "no versions" perfectly well.
            return []
        versions = await self._anthropic.list_versions(remote_id)
        return [AgentVersionResponse(**version) for version in versions]

    # ── writes ────────────────────────────────────────────────────────────────

    async def create_agent(
        self, auth: AuthContext, body: AgentCreateRequest
    ) -> AgentResponse:
        """Create the Anthropic agent, then store the row that mirrors it.

        Vendor first, then store — see the module docstring for why that ordering
        and not the reverse.
        """
        workspace_id, user_id, role = _require_dashboard_user(auth)

        # ── phase 1: network, no transaction open ────────────────────────────
        remote = await self._anthropic.create_agent(
            name=body.name,
            model=_model_config(body.model, body.effort),
            system=body.system_prompt,
            description=body.description,
        )

        agent_id = generate_id("agent")

        # ── phase 2: transaction ─────────────────────────────────────────────
        try:
            async with get_tenant_session() as session, run_in_tenant(
                session, workspace_id, user_id, role
            ) as tenant:
                row = await self._repo.insert(
                    tenant,
                    agent_id=agent_id,
                    workspace_id=workspace_id,
                    owner_user_id=user_id,
                    name=body.name,
                    description=body.description,
                    system_prompt=body.system_prompt,
                    model=body.model,
                    effort=body.effort,
                    ground_in_brain=body.ground_in_brain,
                    budget_cents=body.budget_cents,
                    anthropic_agent_id=remote["id"],
                    anthropic_agent_version=remote["version"],
                )
                await tenant.commit()
        except Exception:
            # Name the orphan explicitly: it is the one inconsistency this
            # ordering can produce, and an operator reading logs needs the id.
            log.exception(
                "agent create: stored row failed after Anthropic agent %s was "
                "created — that agent is now orphaned",
                remote["id"],
            )
            raise

        return _to_response(row, caller_user_id=user_id)

    async def update_agent(
        self, auth: AuthContext, agent_id: str, body: AgentUpdateRequest
    ) -> AgentResponse:
        """Apply a partial update locally and to Anthropic, minting a new version.

        Read, then call, then write — three steps rather than one transaction,
        because the middle one is network I/O (§7).
        """
        workspace_id, user_id, role = _require_dashboard_user(auth)

        # ── phase 1: read the current row (short transaction) ────────────────
        current = await self._load(workspace_id, user_id, role, agent_id)
        if current["owner_user_id"] != user_id:
            # RLS lets a member *read* a published agent; editing it is the
            # owner's alone. Caught here so the caller gets 403 rather than a
            # confusing 404 from an UPDATE that matched nothing.
            raise ForbiddenError("Only the agent's owner can edit it.")

        sent = body.model_dump(exclude_unset=True)
        sent.pop("version", None)

        # ── phase 2: network, no transaction open ────────────────────────────
        remote_version: int | None = current["anthropic_agent_version"]
        remote_id: str | None = current["anthropic_agent_id"]
        if _touches_runtime(sent):
            merged = {**current, **sent}
            if remote_id:
                remote = await self._anthropic.update_agent(
                    remote_id,
                    version=body.version,
                    name=merged["name"],
                    model=_model_config(merged["model"], merged["effort"]),
                    system=merged["system_prompt"],
                    description=merged["description"],
                )
            else:
                # A row whose earlier sync failed. Create now rather than
                # carrying a permanently unrunnable agent.
                remote = await self._anthropic.create_agent(
                    name=merged["name"],
                    model=_model_config(merged["model"], merged["effort"]),
                    system=merged["system_prompt"],
                    description=merged["description"],
                )
            remote_id, remote_version = remote["id"], remote["version"]

        # ── phase 3: transaction ─────────────────────────────────────────────
        fields = dict(sent)
        fields["anthropic_agent_id"] = remote_id
        fields["anthropic_agent_version"] = remote_version

        async with get_tenant_session() as session, run_in_tenant(
            session, workspace_id, user_id, role
        ) as tenant:
            row = await self._repo.update(
                tenant, workspace_id=workspace_id, agent_id=agent_id, fields=fields
            )
            if row is None:
                raise NotFoundError("Agent")
            await tenant.commit()

        return _to_response(row, caller_user_id=user_id)

    async def set_visibility(
        self, auth: AuthContext, agent_id: str, *, visibility: str
    ) -> AgentResponse:
        """Publish to the workspace, or take it back private.

        No Anthropic call: visibility is ours alone — the vendor has no concept
        of who in a workspace may see an agent.
        """
        workspace_id, user_id, role = _require_dashboard_user(auth)
        async with get_tenant_session() as session, run_in_tenant(
            session, workspace_id, user_id, role
        ) as tenant:
            row = await self._repo.set_visibility(
                tenant,
                workspace_id=workspace_id,
                agent_id=agent_id,
                visibility=visibility,
            )
            if row is None:
                # Either invisible or not ours — the RLS WITH CHECK blocks a
                # non-owner's write, so both arrive here as "no row".
                raise NotFoundError("Agent")
            await tenant.commit()
        return _to_response(row, caller_user_id=user_id)

    # ── connectors ────────────────────────────────────────────────────────────

    async def list_connectors(
        self, auth: AuthContext, agent_id: str
    ) -> list[AgentConnectorResponse]:
        workspace_id, user_id, role = _require_dashboard_user(auth)
        await self._load(workspace_id, user_id, role, agent_id)
        rows = await self._connectors(workspace_id, user_id, role, agent_id)
        return [_to_connector(row) for row in rows]

    async def add_connector(
        self, auth: AuthContext, agent_id: str, body: AgentConnectorCreateRequest
    ) -> AgentConnectorResponse:
        """Declare an MCP server, then push the agent's whole tool config.

        The push is not optional bookkeeping: a connector row Anthropic has
        never heard of is decorative, and the agent would run without the tool
        its owner just added. So the new set is computed in memory, sent, and
        only then written — vendor-first, same ordering and same reasoning as
        ``create_agent``.
        """
        workspace_id, user_id, role = _require_dashboard_user(auth)
        agent = await self._load(workspace_id, user_id, role, agent_id)
        if agent["owner_user_id"] != user_id:
            raise ForbiddenError("Only the agent's owner can change its connectors.")

        existing = await self._connectors(workspace_id, user_id, role, agent_id)
        if len(existing) >= MAX_CONNECTORS_PER_AGENT:
            raise ConflictError(
                f"An agent can have at most {MAX_CONNECTORS_PER_AGENT} connectors."
            )
        if any(row["name"] == body.name for row in existing):
            # Caught here as well as by the unique constraint: this way the
            # caller gets the 409 *before* we spend an Anthropic round-trip.
            raise ConflictError(f"This agent already has a connector named {body.name!r}.")

        connector_id = generate_id("agent_connector")
        pending = {
            "id": connector_id,
            "name": body.name,
            "mcp_server_url": body.mcp_server_url,
            "tool_allowlist": body.tool_allowlist,
        }

        # ── phase 1: network, no transaction open ────────────────────────────
        remote_id, remote_version = await self._push_tools(agent, [*existing, pending])

        # ── phase 2: transaction ─────────────────────────────────────────────
        try:
            async with get_tenant_session() as session, run_in_tenant(
                session, workspace_id, user_id, role
            ) as tenant:
                row = await self._repo.insert_connector(
                    tenant,
                    connector_id=connector_id,
                    workspace_id=workspace_id,
                    agent_id=agent_id,
                    name=body.name,
                    mcp_server_url=body.mcp_server_url,
                    provider=body.provider,
                    tool_allowlist=body.tool_allowlist,
                )
                await self._repo.update(
                    tenant,
                    workspace_id=workspace_id,
                    agent_id=agent_id,
                    fields={
                        "anthropic_agent_id": remote_id,
                        "anthropic_agent_version": remote_version,
                    },
                )
                await tenant.commit()
        except IntegrityError as exc:
            # Lost a race on (agent_id, name) between the check above and here.
            raise ConflictError(
                f"This agent already has a connector named {body.name!r}."
            ) from exc

        return _to_connector(row)

    async def remove_connector(
        self, auth: AuthContext, agent_id: str, connector_id: str
    ) -> None:
        """Undeclare an MCP server and push the reduced tool config."""
        workspace_id, user_id, role = _require_dashboard_user(auth)
        agent = await self._load(workspace_id, user_id, role, agent_id)
        if agent["owner_user_id"] != user_id:
            raise ForbiddenError("Only the agent's owner can change its connectors.")

        existing = await self._connectors(workspace_id, user_id, role, agent_id)
        remaining = [row for row in existing if row["id"] != connector_id]
        if len(remaining) == len(existing):
            raise NotFoundError("Connector")

        # ── phase 1: network, no transaction open ────────────────────────────
        remote_id, remote_version = await self._push_tools(agent, remaining)

        # ── phase 2: transaction ─────────────────────────────────────────────
        async with get_tenant_session() as session, run_in_tenant(
            session, workspace_id, user_id, role
        ) as tenant:
            await self._repo.delete_connector(
                tenant,
                workspace_id=workspace_id,
                agent_id=agent_id,
                connector_id=connector_id,
            )
            await self._repo.update(
                tenant,
                workspace_id=workspace_id,
                agent_id=agent_id,
                fields={
                    "anthropic_agent_id": remote_id,
                    "anthropic_agent_version": remote_version,
                },
            )
            await tenant.commit()

    async def _push_tools(
        self, agent: dict[str, Any], connectors: list[dict[str, Any]]
    ) -> tuple[str | None, int | None]:
        """Send the agent's full tool + MCP-server config. Returns the new version.

        Both arrays are replaced wholesale by Anthropic, so every write sends
        the complete set — there is no incremental add.
        """
        tools = _agent_tools(connectors)
        servers = _mcp_servers(connectors)
        remote_id = agent["anthropic_agent_id"]
        if remote_id:
            remote = await self._anthropic.update_agent(
                remote_id, tools=tools, mcp_servers=servers
            )
        else:
            remote = await self._anthropic.create_agent(
                name=agent["name"],
                model=_model_config(agent["model"], agent["effort"]),
                system=agent["system_prompt"],
                description=agent["description"],
                tools=tools,
                mcp_servers=servers,
            )
        return remote["id"], remote["version"]

    async def _connectors(
        self, workspace_id: str, user_id: str, role: str, agent_id: str
    ) -> list[dict[str, Any]]:
        async with get_tenant_session() as session, run_in_tenant(
            session, workspace_id, user_id, role
        ) as tenant:
            return await self._repo.list_connectors(
                tenant, workspace_id=workspace_id, agent_id=agent_id
            )

    # ── internals ─────────────────────────────────────────────────────────────

    async def _load(
        self, workspace_id: str, user_id: str, role: str, agent_id: str
    ) -> dict[str, Any]:
        """Load one visible agent or raise 404. Its own short transaction."""
        async with get_tenant_session() as session, run_in_tenant(
            session, workspace_id, user_id, role
        ) as tenant:
            row = await self._repo.get(
                tenant, workspace_id=workspace_id, agent_id=agent_id
            )
        if row is None:
            raise NotFoundError("Agent")
        return row


# Fields Anthropic actually stores. A save touching only ours — visibility,
# grounding, budget, status — must not mint a vendor version: versions are the
# agent's audit trail, and filling it with local bookkeeping makes rollback
# useless.
_RUNTIME_FIELDS = frozenset({"name", "model", "effort", "system_prompt", "description"})


def _touches_runtime(sent: dict[str, Any]) -> bool:
    return bool(_RUNTIME_FIELDS & sent.keys())


# The pre-built Claude agent toolset: bash, file ops, code execution inside the
# session's container. Always present — an agent with only MCP tools has no way
# to do anything with what it fetches.
_AGENT_TOOLSET = {"type": "agent_toolset_20260401"}


def _mcp_servers(connectors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Anthropic's ``mcp_servers``: declarations only, deliberately no auth field.

    Credentials reach the server from the user's vault, matched by URL at
    session create (phase 2). That split is what keeps secrets out of a
    reusable agent definition.
    """
    return [
        {"type": "url", "name": row["name"], "url": row["mcp_server_url"]}
        for row in connectors
    ]


def _agent_tools(connectors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The agent's ``tools``: the built-in toolset plus one entry per connector.

    An empty ``tool_allowlist`` means "every tool this server exposes", which is
    the right default even though the allowlist is the safer shape: we cannot
    know a pasted server's tool names before connecting to it, so defaulting to
    an allowlist would narrow every new connector down to nothing.
    """
    tools: list[dict[str, Any]] = [dict(_AGENT_TOOLSET)]
    for row in connectors:
        toolset: dict[str, Any] = {
            "type": "mcp_toolset",
            "mcp_server_name": row["name"],
        }
        allowlist = row.get("tool_allowlist") or []
        if allowlist:
            toolset["default_config"] = {"enabled": False}
            toolset["configs"] = [{"name": tool, "enabled": True} for tool in allowlist]
        tools.append(toolset)
    return tools


def _to_connector(row: dict[str, Any]) -> AgentConnectorResponse:
    return AgentConnectorResponse(
        id=row["id"],
        agent_id=row["agent_id"],
        name=row["name"],
        mcp_server_url=row["mcp_server_url"],
        provider=row.get("provider"),
        tool_allowlist=row.get("tool_allowlist") or [],
        created_at=row.get("created_at"),
    )
