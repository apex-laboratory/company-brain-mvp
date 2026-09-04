"""Agent-builder API contract (agent-builder-plan §5.3, phase 1).

This is the half of phase 1 the frontend is blocked on, so the shapes here are
the contract more than they are validation: `features/agents/` builds its list,
builder form and version list against exactly these fields.

Two things are deliberate:

* **`model` and `effort` are closed sets, not free strings.** The alternative is
  discovering a typo when Anthropic 400s at save time, having already charged the
  user a round-trip; a 422 naming the field is cheaper and happens before any
  vendor call. The list is Claude 4.5+ because that is what Managed Agents runs.
* **Update is tri-state.** PATCH must tell "leave this alone" apart from "clear
  it", so `AgentUpdateRequest` is read through ``model_dump(exclude_unset=True)``
  and an explicit ``null`` is a real value. Collapsing the two would make every
  partial save silently wipe the fields it did not mention.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, ValidationInfo, field_validator

from app.shared.schemas import CamelModel, CamelRequestModel

# Managed Agents supports Claude 4.5+. Kept explicit rather than free-form: see
# the module docstring. Opus 5 is the default — never downgrade a user's agent
# for cost, that is their call to make in the builder.
AgentModel = Literal[
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-haiku-4-5",
]

# Effort is agent configuration only. Anthropic silently ignores an effort set in
# a per-session override, so it lives here and changing it means a new agent
# version — which is also why it is worth validating before the vendor call.
AgentEffort = Literal["low", "medium", "high", "xhigh", "max"]

Visibility = Literal["private", "workspace"]
AgentStatus = Literal["draft", "active", "archived"]


class AgentResponse(CamelModel):
    """One agent as the dashboard sees it.

    ``anthropic_agent_version`` is exposed as ``version`` because that is what a
    caller passes back to PATCH for optimistic concurrency — naming it after the
    vendor would make the round-trip read like an implementation leak.
    """

    id: str
    name: str
    description: str | None = None
    system_prompt: str | None = None
    model: str | None = None
    effort: str | None = None
    ground_in_brain: bool = True
    budget_cents: int | None = None
    visibility: Visibility = "private"
    status: AgentStatus = "draft"
    owner_user_id: str
    # Lets the list render *Yours* vs *Shared in {workspace}* without the client
    # re-deriving ownership from a user id it would otherwise not need.
    is_owner: bool = False
    version: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class AgentVersionResponse(CamelModel):
    """One entry of the agent's append-only history, proxied from Anthropic.

    Not mirrored locally on purpose: version history is Anthropic's record, and a
    local copy is one failed write away from disagreeing with it.
    """

    version: int | None = None
    name: str | None = None
    created_at: datetime | None = None


class AgentCreateRequest(CamelRequestModel):
    """``POST /agents``. Only ``name`` is required — the builder saves early."""

    name: Annotated[str, Field(min_length=1, max_length=256)]
    description: Annotated[str | None, Field(default=None, max_length=2048)] = None
    # Anthropic caps the system prompt at 100K chars; rejecting here means the
    # user hears about it before we spend a round-trip finding out.
    system_prompt: Annotated[str | None, Field(default=None, max_length=100_000)] = None
    model: AgentModel = "claude-opus-5"
    effort: AgentEffort | None = None
    # On by default: grounding *is* the product (§10). A builder that defaulted
    # this off would ship an Anthropic agent builder with a wiki attached.
    ground_in_brain: bool = True
    budget_cents: Annotated[int | None, Field(default=None, gt=0)] = None


class AgentUpdateRequest(CamelRequestModel):
    """``PATCH /agents/{id}``. Every field optional; unset means "leave alone".

    ``version`` is optimistic concurrency, passed straight to Anthropic. Send it
    to get a 409 when someone else has edited the agent since you loaded it;
    omit it to apply unconditionally, which is last-write-wins and appropriate
    only for a declarative apply loop that owns the agent.

    Note Anthropic 409s on a stale ``version`` **even when the fields you send
    already equal the stored values**, so a no-op save is still a conflict.
    """

    name: Annotated[str | None, Field(default=None, min_length=1, max_length=256)] = None
    description: Annotated[str | None, Field(default=None, max_length=2048)] = None
    system_prompt: Annotated[str | None, Field(default=None, max_length=100_000)] = None
    model: AgentModel | None = None
    effort: AgentEffort | None = None
    ground_in_brain: bool | None = None
    budget_cents: Annotated[int | None, Field(default=None, gt=0)] = None
    status: AgentStatus | None = None
    version: Annotated[int | None, Field(default=None, ge=1)] = None


# Anthropic caps an agent at 20 MCP servers (§8). Enforced here so the picker
# says so plainly rather than letting a save fail at the vendor.
MAX_CONNECTORS_PER_AGENT = 20

# ``name`` is not decoration: it is the key ``mcp_toolset.mcp_server_name``
# points at, so it has to survive a round-trip through Anthropic's config
# unchanged. Restricting it to a slug avoids finding out at save time which
# characters that config rejects.
_CONNECTOR_NAME = r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$"


class AgentConnectorResponse(CamelModel):
    """One MCP server this agent talks to. Never any credential material."""

    id: str
    agent_id: str
    name: str
    mcp_server_url: str
    provider: str | None = None
    tool_allowlist: list[str] = []
    created_at: datetime | None = None


class AgentConnectorCreateRequest(CamelRequestModel):
    """``POST /agents/{id}/connectors``.

    ``mcp_server_url`` must be ``https``. Anthropic connects to it over
    Streamable HTTP carrying a vault credential, and a plaintext hop would put
    that credential on the wire. We never fetch this URL ourselves — the
    connection is made from Anthropic's side — so this is a credential-exposure
    check, not an SSRF one.
    """

    name: Annotated[str, Field(pattern=_CONNECTOR_NAME)]
    mcp_server_url: Annotated[str, Field(min_length=1, max_length=2048)]
    # NULL means a pasted custom URL; a catalog key otherwise. The catalog
    # itself is phase 3 — until then every connector is effectively custom.
    provider: Annotated[str | None, Field(default=None, max_length=64)] = None
    # Empty means "every tool this server exposes". A non-empty list becomes
    # Anthropic's `default_config: {enabled: false}` + per-tool `configs`
    # allowlist, which is the safer shape but a worse default: a server whose
    # tool names we cannot know yet would be allowlisted down to nothing.
    tool_allowlist: list[Annotated[str, Field(max_length=128)]] = []

    @field_validator("mcp_server_url")
    @classmethod
    def _must_be_https(cls, url: str) -> str:
        if not url.startswith("https://"):
            raise ValueError("mcpServerUrl must be an https:// URL")
        return url


# ── catalog + credentials (phase 2) ───────────────────────────────────────────


class ConnectorCatalogEntry(CamelModel):
    """One curated connector the picker can offer.

    ``configured`` is not decoration: a deployment that has not registered an
    OAuth client for a provider still lists it, so the picker can render it
    disabled with a reason rather than letting the user start a consent flow that
    ends in a 501. The frontend is expected to honour it.
    """

    provider: str
    display_name: str
    description: str | None = None
    mcp_server_url: str
    docs_url: str | None = None
    # The scope string the consent screen will ask for, so the builder can tell
    # the user what they are about to grant before they grant it.
    scopes: str | None = None
    configured: bool = True
    # Whether the *calling user* has already authorized this provider. Part of
    # the catalog rather than a second request because every surface that renders
    # the picker needs both, and two calls means a frame where a connected
    # provider renders as "Connect".
    connected: bool = False


class AgentCredentialResponse(CamelModel):
    """One provider the calling user has connected. **Never any token material.**

    There is no field here that could carry a secret, and that is enforced a
    layer down: ``agent_credentials`` has no column to put one in (migration
    0028). ``anthropic_credential_id`` is a pointer, not a credential.
    """

    id: str
    provider: str
    display_name: str | None = None
    mcp_server_url: str
    connected_at: datetime | None = None


class UnauthorizedConnector(CamelModel):
    """An agent connector the calling user has no credential for.

    This is the "Needs your GitHub account" row. It names the agent as well as
    the connector because the user meets it in two places — on an agent's card
    and on the credentials screen — and only one of those already knows which
    agent is asking.
    """

    agent_id: str
    agent_name: str
    connector_id: str
    connector_name: str
    provider: str | None = None
    mcp_server_url: str


class AgentCredentialsOverview(CamelModel):
    """``GET /agent-credentials`` — what this user has, and what they still need."""

    connections: list[AgentCredentialResponse] = []
    needs_authorization: list[UnauthorizedConnector] = []


class AgentAuthorizeStartRequest(CamelRequestModel):
    """``POST /agent-credentials/{provider}/authorize``.

    ``returnTo`` picks where the callback lands the browser. It ends up in a
    ``Location`` header, so it is validated against a strict pattern and anything
    that fails degrades to the agents list rather than 422 — a stale frontend
    build must still be able to complete a connect.
    """

    return_to: Annotated[str | None, Field(default=None, max_length=256)] = None


class AgentAuthorizeStartResponse(CamelModel):
    """The provider consent URL to redirect the browser to.

    Same shape as ``/sources/{provider}/authorize`` so the frontend's existing
    full-page-redirect hook works unchanged.
    """

    authorize_url: str


# ── sessions (phase 4) ────────────────────────────────────────────────────────
#
# **§5.6's session budget does not exist.** The plan specifies
# ``budget={"type": "limit", "max_list_cost": …}`` on every session, and there is
# no such parameter in Managed Agents as shipped: no ``budget`` on
# ``sessions.create``, none on ``deployments.create``, and no cost field on the
# session object to read one back from. That was written against a surface the
# vendor has not exposed, so the cost brake has to be ours.
#
# What replaces it is deliberately cruder and actually enforceable: a cap on how
# many of a user's sessions may be live at once. It does not bound spend per
# session — nothing available to us does — but it does bound the failure mode
# that costs real money, which is sessions accumulating faster than anyone
# notices. ``agent_definitions.budget_cents`` stays in the schema for the day the
# parameter lands; until then it is recorded and not enforced, and saying so here
# is cheaper than someone discovering it from an invoice.

# Per user, not per workspace: ``agent_sessions``' RLS is personal, so a
# workspace-wide count is not something a member's transaction can even see.
MAX_LIVE_SESSIONS_PER_USER = 5

# The first message is required. A session is a running container; creating one
# with nothing to do burns the environment's resources until it idles out, and
# there is no interactive affordance that would ever want it.
_MAX_MESSAGE_CHARS = 100_000

SessionStatus = Literal["rescheduling", "running", "idle", "terminated"]


class AgentSessionResponse(CamelModel):
    """One run of an agent, as the dashboard sees it. **Never any transcript.**

    Every field here is a pointer or a counter, and that is enforced a layer
    down: ``agent_sessions`` has no column to put message content in (migration
    0028). The transcript is fetched from ``/agent-sessions/{id}/events``, which
    proxies Anthropic and stores nothing.
    """

    id: str
    agent_id: str
    title: str | None = None
    status: SessionStatus | None = None
    # The discriminator inside Anthropic's ``session.status_idle`` stop reason —
    # ``end_turn``, ``requires_action``, ``retries_exhausted``. The frontend needs
    # it to tell "finished" from "waiting for you to approve a tool", which is the
    # idle-gate trap in §6.2 and the difference between a done run and a frozen UI.
    stop_reason: str | None = None
    # The agent version this session is pinned to. An agent edited mid-session
    # does not retroactively change what this run executed.
    agent_version: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None


class AgentSessionCreateRequest(CamelRequestModel):
    """``POST /agents/{id}/sessions`` — start a run and say the first thing."""

    message: Annotated[str, Field(min_length=1, max_length=_MAX_MESSAGE_CHARS)]
    title: Annotated[str | None, Field(default=None, max_length=256)] = None


class UserMessageEvent(CamelRequestModel):
    """Say something to a running agent.

    Text only. Anthropic's user message takes image and document blocks too, and
    accepting them would mean forwarding caller-supplied vendor structures we do
    not validate — a wider surface than the builder has any use for yet.
    """

    type: Literal["user.message"]
    text: Annotated[str, Field(min_length=1, max_length=_MAX_MESSAGE_CHARS)]


class UserInterruptEvent(CamelRequestModel):
    """Stop the agent mid-turn. It goes idle rather than terminating."""

    type: Literal["user.interrupt"]


class UserToolConfirmationEvent(CamelRequestModel):
    """Answer a tool-confirmation prompt.

    ``tool_use_id`` comes from the ``event_ids`` on the last
    ``session.status_idle`` whose stop reason is ``requires_action`` — not from
    the tool-use event the client happens to have rendered last, which is a
    different id whenever the agent asked for several tools at once.
    """

    type: Literal["user.tool_confirmation"]
    tool_use_id: Annotated[str, Field(min_length=1, max_length=256)]
    result: Literal["allow", "deny"]
    deny_message: Annotated[str | None, Field(default=None, max_length=2048)] = None

    @field_validator("deny_message")
    @classmethod
    def _only_when_denying(cls, value: str | None, info: ValidationInfo) -> str | None:
        # Anthropic rejects a deny_message on an allow. Caught here so the caller
        # gets a 422 naming the field instead of a 400 after a round-trip.
        if value and info.data.get("result") != "deny":
            raise ValueError("denyMessage is only allowed when result is 'deny'")
        return value


# Discriminated on ``type`` so a body with an unknown kind fails naming the
# field, rather than being tried against each member and reported as three
# simultaneous errors. Deliberately a subset of what the vendor accepts:
# ``user.define_outcome``, ``system.message`` and the custom-tool results have no
# surface in the builder, and exposing them would let a client drive parts of the
# session lifecycle the UI cannot represent.
AgentSessionEvent = Annotated[
    UserMessageEvent | UserInterruptEvent | UserToolConfirmationEvent,
    Field(discriminator="type"),
]


class AgentEventSendRequest(CamelRequestModel):
    """``POST /agent-sessions/{id}/events``. One batch, sent in order."""

    events: Annotated[list[AgentSessionEvent], Field(min_length=1, max_length=20)]
