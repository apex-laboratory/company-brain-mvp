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

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import RedirectResponse, StreamingResponse

from app.jobs.queue import enqueue
from app.modules.agents.credentials import AgentCredentialsService
from app.modules.agents.schemas import (
    AgentAuthorizeStartRequest,
    AgentConnectorCreateRequest,
    AgentCreateRequest,
    AgentEventSendRequest,
    AgentSessionCreateRequest,
    AgentUpdateRequest,
)
from app.modules.agents.service import AgentsService
from app.modules.agents.sessions import AgentSessionsService
from app.modules.agents.webhook import parse, summarize, verify
from app.shared.http.respond import accepted, created, no_content, ok
from app.shared.middleware.authenticate import AuthContext, get_auth_context
from app.shared.middleware.authorize import require_role
from app.shared.middleware.rate_limit import (
    AGENT_WEBHOOK_LIMIT,
    DASHBOARD_LIMIT,
    OAUTH_CALLBACK_LIMIT,
    limiter,
    workspace_key,
)

router = APIRouter(prefix="/agents", tags=["agents"])

# Credentials are the *user's*, not an agent's, so they hang off their own prefix
# rather than under /agents/{id}: one GitHub connection serves every agent that
# user builds, and nesting it would imply a per-agent authorization that does not
# exist and would have to be undone the first time somebody built a second agent.
credentials_router = APIRouter(prefix="/agent-credentials", tags=["agents"])

_service = AgentsService()
_credentials = AgentCredentialsService()


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


@router.get("/catalog")
@limiter.limit(DASHBOARD_LIMIT, key_func=workspace_key)
async def list_catalog(request: Request, auth: AuthContext = Depends(get_auth_context)):
    """The curated connector catalog, annotated with what this caller has connected.

    **Declared before ``/{agent_id}`` on purpose.** FastAPI matches routes in
    declaration order, so moving this below would make ``catalog`` a perfectly
    valid agent id and turn the whole catalog into a 404.
    """
    entries = await _credentials.list_catalog(auth)
    return ok(request, [entry.model_dump(by_alias=True) for entry in entries])


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


# ── connectors ────────────────────────────────────────────────────────────────


@router.get("/{agent_id}/connectors")
@limiter.limit(DASHBOARD_LIMIT, key_func=workspace_key)
async def list_connectors(
    request: Request, agent_id: str, auth: AuthContext = Depends(get_auth_context)
):
    """The MCP servers this agent talks to. No credential material, ever."""
    connectors = await _service.list_connectors(auth, agent_id)
    return ok(request, [c.model_dump(by_alias=True) for c in connectors])


@router.post(
    "/{agent_id}/connectors", dependencies=[Depends(require_role("editor"))]
)
@limiter.limit(DASHBOARD_LIMIT, key_func=workspace_key)
async def add_connector(
    request: Request,
    agent_id: str,
    body: AgentConnectorCreateRequest,
    auth: AuthContext = Depends(get_auth_context),
):
    """Declare an MCP server and push the agent's tool config. Owner only."""
    connector = await _service.add_connector(auth, agent_id, body)
    return created(request, connector.model_dump(by_alias=True))


@router.delete(
    "/{agent_id}/connectors/{connector_id}",
    dependencies=[Depends(require_role("editor"))],
)
@limiter.limit(DASHBOARD_LIMIT, key_func=workspace_key)
async def remove_connector(
    request: Request,
    agent_id: str,
    connector_id: str,
    auth: AuthContext = Depends(get_auth_context),
):
    """Undeclare an MCP server and push the reduced config. Owner only."""
    await _service.remove_connector(auth, agent_id, connector_id)
    return no_content()


# ── credentials ───────────────────────────────────────────────────────────────
#
# Connecting a provider is a **viewer-and-up** action, deliberately weaker than
# the ``editor`` gate on building agents. A viewer can run an agent somebody else
# published, and an agent reaching a connector runs against the credentials of
# whoever is running it — so a viewer who cannot authorize their own accounts can
# see the agent and never use it.


@credentials_router.get("")
@limiter.limit(DASHBOARD_LIMIT, key_func=workspace_key)
async def list_credentials(
    request: Request, auth: AuthContext = Depends(get_auth_context)
):
    """What the caller has connected, plus which agents need something they lack."""
    overview = await _credentials.overview(auth)
    return ok(request, overview.model_dump(by_alias=True))


@credentials_router.post("/{provider}/authorize")
@limiter.limit(DASHBOARD_LIMIT, key_func=workspace_key)
async def authorize_credential(
    provider: str,
    request: Request,
    body: AgentAuthorizeStartRequest | None = None,
    auth: AuthContext = Depends(get_auth_context),
):
    """Begin the OAuth flow: return the provider consent URL to redirect to.

    Same contract as ``POST /sources/{provider}/authorize`` — ``202`` with an
    ``authorizeUrl`` — so the frontend's existing full-page-redirect hook works
    unchanged. ``returnTo`` picks where the callback lands; the ``Origin`` header
    (allowlisted against ``FRONTEND_URLS``) picks which frontend it lands on.
    """
    result = await _credentials.start_authorization(
        auth,
        provider,
        return_to=body.return_to if body else None,
        origin=request.headers.get("origin"),
    )
    return accepted(request, result.model_dump(by_alias=True))


@credentials_router.get("/{provider}/callback")
@limiter.limit(OAUTH_CALLBACK_LIMIT)
async def credential_callback(
    provider: str,
    request: Request,
    state: str,
    code: str | None = None,
    error: str | None = None,
):
    """OAuth redirect target: exchange, vault the token, bounce to the dashboard.

    Unauthenticated by design — it is a browser redirect from the provider and
    carries no JWT. The signed, single-use ``state`` is the credential, and it
    resolves the workspace and user the connection belongs to. ``code`` is absent
    when the user declined; ``error`` carries the provider's reason.

    **The token exchanged here is never written to Postgres.** It goes to the
    user's Anthropic vault and the only thing stored is the id that comes back.
    """
    redirect_to = await _credentials.handle_callback(
        provider, state=state, code=code, error=error
    )
    return RedirectResponse(url=redirect_to, status_code=302)


@credentials_router.delete("/{credential_id}")
@limiter.limit(DASHBOARD_LIMIT, key_func=workspace_key)
async def disconnect_credential(
    credential_id: str,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    """Revoke a connection: delete it from the vault, then forget the pointer.

    Not in the plan's endpoint table, and load-bearing anyway: a vault holds 20
    credentials, so without this a user who connects and abandons providers has
    no way to reclaim a slot — and no way to revoke access they have changed
    their mind about.
    """
    await _credentials.disconnect(auth, credential_id)
    return no_content()


# ── sessions ──────────────────────────────────────────────────────────────────
#
# **Viewer-and-up, like credentials and unlike building.** Running somebody's
# published agent is the point of publishing it, and the run is scoped to the
# caller: their vault supplies the connector credentials, and `agent_sessions`'
# RLS is personal, so a viewer's session is invisible to everyone including the
# agent's owner. Requiring `editor` here would mean an agent could be shared with
# people who could not use it.
#
# Sessions hang off `/agent-sessions/{id}` rather than
# `/agents/{agent_id}/sessions/{id}` for the ordinary reason: the id is already
# unique, and nesting would let a caller pass a mismatched agent id and make the
# route's own authorization check ambiguous. Creation stays nested, because there
# the agent is the thing being acted on.

sessions_router = APIRouter(prefix="/agent-sessions", tags=["agents"])

_sessions = AgentSessionsService()


@router.post("/{agent_id}/sessions")
@limiter.limit(DASHBOARD_LIMIT, key_func=workspace_key)
async def create_session(
    request: Request,
    agent_id: str,
    body: AgentSessionCreateRequest,
    auth: AuthContext = Depends(get_auth_context),
):
    """Start a run of this agent, with the caller's vaults attached, and speak first."""
    session = await _sessions.create_session(auth, agent_id, body)
    return created(request, session.model_dump(by_alias=True))


@router.get("/{agent_id}/sessions")
@limiter.limit(DASHBOARD_LIMIT, key_func=workspace_key)
async def list_agent_sessions(
    request: Request,
    agent_id: str,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    auth: AuthContext = Depends(get_auth_context),
):
    """The caller's own runs of this agent, newest first."""
    sessions = await _sessions.list_sessions(auth, agent_id=agent_id, limit=limit)
    return ok(request, [s.model_dump(by_alias=True) for s in sessions])


@sessions_router.get("")
@limiter.limit(DASHBOARD_LIMIT, key_func=workspace_key)
async def list_sessions(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    auth: AuthContext = Depends(get_auth_context),
):
    """Every run of the caller's, across agents."""
    sessions = await _sessions.list_sessions(auth, limit=limit)
    return ok(request, [s.model_dump(by_alias=True) for s in sessions])


@sessions_router.get("/{session_id}")
@limiter.limit(DASHBOARD_LIMIT, key_func=workspace_key)
async def get_session(
    request: Request, session_id: str, auth: AuthContext = Depends(get_auth_context)
):
    """One run's status and usage. Somebody else's session is a 404, not a 403."""
    session = await _sessions.get_session(auth, session_id)
    return ok(request, session.model_dump(by_alias=True))


@sessions_router.get("/{session_id}/stream")
@limiter.limit(DASHBOARD_LIMIT, key_func=workspace_key)
async def stream_session(
    request: Request, session_id: str, auth: AuthContext = Depends(get_auth_context)
):
    """Pass-through of Anthropic's live event stream. Nothing is stored.

    The upstream connection is opened *before* this returns a response, so an
    unknown or someone else's session still renders as an ordinary 404 envelope
    rather than a 200 with a silent empty body — Starlette commits the status
    line before pulling the first chunk of a ``StreamingResponse``.
    """
    frames = await _sessions.open_stream(auth, session_id)
    return StreamingResponse(
        frames,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@sessions_router.get("/{session_id}/events")
@limiter.limit(DASHBOARD_LIMIT, key_func=workspace_key)
async def list_session_events(
    request: Request,
    session_id: str,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    order: Annotated[Literal["asc", "desc"], Query()] = "asc",
    page: Annotated[str | None, Query(max_length=2048)] = None,
    after: Annotated[str | None, Query(max_length=64)] = None,
    auth: AuthContext = Depends(get_auth_context),
):
    """Replay proxy for reconnect — **proxied verbatim, never stored**.

    SSE has no replay, so reopening a stream starts from "now" and silently
    loses the gap. §6.2's recipe is to open the stream first and then call this
    to backfill, deduping on event id as the live stream catches up; ``after``
    is an RFC-3339 timestamp bounding that backfill.

    The body is Anthropic's, forwarded unchanged, cursor included. It is not
    reshaped into a ``CamelModel`` on purpose: the event union is open-ended at
    the vendor, and typing it here would drop the first event kind they add.
    """
    events = await _sessions.list_events(
        auth, session_id, limit=limit, order=order, page=page, after=after
    )
    return ok(request, events)


@sessions_router.post("/{session_id}/events")
@limiter.limit(DASHBOARD_LIMIT, key_func=workspace_key)
async def send_session_events(
    request: Request,
    session_id: str,
    body: AgentEventSendRequest,
    auth: AuthContext = Depends(get_auth_context),
):
    """Say something, interrupt, or answer a tool-confirmation prompt."""
    await _sessions.send_events(auth, session_id, body)
    return no_content()


@sessions_router.delete("/{session_id}")
@limiter.limit(DASHBOARD_LIMIT, key_func=workspace_key)
async def archive_session(
    request: Request, session_id: str, auth: AuthContext = Depends(get_auth_context)
):
    """End a run for good and free one of the caller's concurrent-session slots.

    ``DELETE`` rather than a ``/stop`` POST because it is terminal — Anthropic
    has no unarchive — and because the row survives as history either way. This
    is the only thing that actually ends the spend; ``user.interrupt`` pauses a
    turn.
    """
    await _sessions.archive_session(auth, session_id)
    return no_content()


async def close_agents_http_client() -> None:
    """Shut down the module's raw-proxy HTTP client at app shutdown.

    The routers hold process-lifetime service instances, and two vendor calls
    (the events list and the session stream) go through httpx rather than the
    SDK, so there is a connection pool here that nothing else would close.
    """
    await _sessions._anthropic.aclose()  # noqa: SLF001 — same module's own client


# ── the Anthropic webhook ─────────────────────────────────────────────────────
#
# **Registered before ``webhooks_router`` in main.py, and that ordering is
# load-bearing.** The sources module owns ``POST /webhooks/{provider}``, which
# matches ``/webhooks/anthropic`` perfectly well; FastAPI resolves in
# registration order, so if this router went second every Anthropic delivery
# would reach the source-integration dispatcher and 404 as an unknown provider.
# A test asserts the resolution rather than the registration, because the
# registration is the thing somebody would reorder while tidying imports.
#
# It lives here rather than in the webhooks module because the payload is an
# agent-builder concern end to end — that module dispatches to
# ``app/integrations``, and Anthropic is not a source we ingest from.

webhook_router = APIRouter(prefix="/webhooks", tags=["agents"])


@webhook_router.post("/anthropic")
@limiter.limit(AGENT_WEBHOOK_LIMIT)
async def anthropic_webhook(request: Request):
    """Verify a session-state delivery and queue the mirror update.

    Unauthenticated by design and verified by signature instead — a vendor
    callback carries no credential of ours. The raw body is read **before**
    anything parses it, because the HMAC covers the exact bytes Anthropic signed
    and re-serializing JSON would reorder keys.

    Always ``200`` once verified, even for an event we do not handle. A 4xx would
    make Anthropic retry a delivery that cannot ever succeed, and an endpoint
    that retries forever on events it ignores eventually starves the ones it
    does. The response says only whether it was accepted — never anything about
    the session, which would let a forged id be used as an oracle.
    """
    raw_body = await request.body()
    verify(request.headers, raw_body)
    parsed = parse(raw_body)
    if parsed is not None:
        delivery_id, event_type, session_id = parsed
        await enqueue(
            "sync_agent_session",
            session_id,
            event_type,
            # Anthropic's own delivery id, so a redelivery inside the result
            # window collapses instead of racing itself through the mirror.
            _job_id=f"agent-session-sync:{delivery_id}",
        )
    return ok(request, summarize(parsed))
