"""Agent-builder routes (agent-builder-plan §5.3, phase 1).

Paths and dependencies only — every decision lives in the service.

**Dashboard-only, by role rather than by scope.** There is no agent-key path
here: building an agent is a person's action, and `agent_definitions`' RLS
compares `owner_user_id` to `current_user_id()`, which an API key has no value
for. `require_role("editor")` on the writes matches the rest of the dashboard —
a viewer can see the workspace's published agents and run them, but not author
one.

Publish and unpublish are separate POSTs rather than a `visibility` field on
PATCH. They are the one change a non-owner must never make, and a distinct route
is what lets that be enforced and audited as its own action instead of hiding
inside a general update.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.modules.agents.schemas import AgentCreateRequest, AgentUpdateRequest
from app.modules.agents.service import AgentsService
from app.shared.http.respond import created, ok
from app.shared.middleware.authenticate import AuthContext, get_auth_context
from app.shared.middleware.authorize import require_role
from app.shared.middleware.rate_limit import DASHBOARD_LIMIT, limiter, workspace_key

router = APIRouter(prefix="/agents", tags=["agents"])

_service = AgentsService()


@router.get("")
@limiter.limit(DASHBOARD_LIMIT, key_func=workspace_key)
async def list_agents(request: Request, auth: AuthContext = Depends(get_auth_context)):
    """Agents visible to the caller — own private plus workspace-published."""
    agents = await _service.list_agents(auth)
    return ok(request, [agent.model_dump(by_alias=True) for agent in agents])


@router.post("", dependencies=[Depends(require_role("editor"))])
@limiter.limit(DASHBOARD_LIMIT, key_func=workspace_key)
async def create_agent(
    request: Request,
    body: AgentCreateRequest,
    auth: AuthContext = Depends(get_auth_context),
):
    """Create an agent. Provisions the Anthropic agent, then stores the mirror."""
    agent = await _service.create_agent(auth, body)
    return created(request, agent.model_dump(by_alias=True))


@router.get("/{agent_id}")
@limiter.limit(DASHBOARD_LIMIT, key_func=workspace_key)
async def get_agent(
    request: Request, agent_id: str, auth: AuthContext = Depends(get_auth_context)
):
    """One agent. Invisible and nonexistent are both 404 — see the repository."""
    agent = await _service.get_agent(auth, agent_id)
    return ok(request, agent.model_dump(by_alias=True))


@router.patch("/{agent_id}", dependencies=[Depends(require_role("editor"))])
@limiter.limit(DASHBOARD_LIMIT, key_func=workspace_key)
async def update_agent(
    request: Request,
    agent_id: str,
    body: AgentUpdateRequest,
    auth: AuthContext = Depends(get_auth_context),
):
    """Partial update. Pass ``version`` for a 409 on a concurrent edit."""
    agent = await _service.update_agent(auth, agent_id, body)
    return ok(request, agent.model_dump(by_alias=True))


@router.get("/{agent_id}/versions")
@limiter.limit(DASHBOARD_LIMIT, key_func=workspace_key)
async def list_agent_versions(
    request: Request, agent_id: str, auth: AuthContext = Depends(get_auth_context)
):
    """Proxy the agent's version history. Empty for a draft that never synced."""
    versions = await _service.list_versions(auth, agent_id)
    return ok(request, [version.model_dump(by_alias=True) for version in versions])


@router.post("/{agent_id}/publish", dependencies=[Depends(require_role("editor"))])
@limiter.limit(DASHBOARD_LIMIT, key_func=workspace_key)
async def publish_agent(
    request: Request, agent_id: str, auth: AuthContext = Depends(get_auth_context)
):
    """Make this agent visible to the whole workspace. Owner only."""
    agent = await _service.set_visibility(auth, agent_id, visibility="workspace")
    return ok(request, agent.model_dump(by_alias=True))


@router.post("/{agent_id}/unpublish", dependencies=[Depends(require_role("editor"))])
@limiter.limit(DASHBOARD_LIMIT, key_func=workspace_key)
async def unpublish_agent(
    request: Request, agent_id: str, auth: AuthContext = Depends(get_auth_context)
):
    """Take it back to private. Owner only — the inverse of publish."""
    agent = await _service.set_visibility(auth, agent_id, visibility="private")
    return ok(request, agent.model_dump(by_alias=True))
