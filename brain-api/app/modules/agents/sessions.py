"""Agent session lifecycle (agent-builder-plan §5.3, §5.4, phase 4).

A session is a running container with an agent in it. Everything here is about
starting one, talking to it, watching it, and stopping it — and about doing all
of that while storing **nothing it says**. ``agent_sessions`` holds pointers and
counters; the transcript is proxied from Anthropic on demand and never lands in
Postgres (migration 0028, §5.5).

Three shapes recur and are worth stating once.

**Vendor first, then store**, as everywhere else in this module: Anthropic is the
source of truth and the local row is a mirror. Session create is the one place
that ordering is genuinely awkward, because there are two vendor calls with the
insert between them — see ``create_session``.

**Every vendor call happens with no transaction open** (``BACKEND_BEST_PRACTICES``
§7). Session create makes four round-trips; a pooled connection pinned across
them would be held for seconds.

**A session id is not an authorization.** Every method loads the local row first,
inside a tenant transaction, and RLS on ``agent_sessions`` is *personal* —
``workspace_id = current_workspace_id() AND user_id = current_user_id()``. So a
member cannot read, stream, or send into another member's run of the same
published agent, and a row that is not the caller's comes back as no row at all.
Skipping that load and going straight to the vendor with a session id would turn
every one of these routes into a cross-tenant read, because the Anthropic call
carries no workspace scoping of its own.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from app.config.database import get_session, get_tenant_session
from app.modules.agents.anthropic_client import AnthropicAgentsClient
from app.modules.agents.credentials import AgentCredentialsService
from app.modules.agents.repository import AgentsRepository
from app.modules.agents.schemas import (
    MAX_LIVE_SESSIONS_PER_USER,
    AgentEventSendRequest,
    AgentSessionCreateRequest,
    AgentSessionResponse,
)
from app.modules.agents.service import _require_dashboard_user
from app.modules.agents.stream import open_session_stream
from app.shared.errors.app_error import ConflictError, NotFoundError
from app.shared.helpers.ids import generate_id
from app.shared.middleware.authenticate import AuthContext
from app.shared.middleware.with_tenant import run_in_tenant

log = logging.getLogger(__name__)

# Anthropic's webhook types, mapped onto what the mirror records. The vendor
# sends only ``{id, type}``, so the type is the only signal in the delivery
# itself — usage and status are read back afterwards.
#
# ``requires_action`` is the one that earns its place: an idle session waiting
# for a tool confirmation and an idle session that has finished its turn are the
# same ``status`` and completely different things to render. §6.2 calls treating
# them alike the idle-gate trap, and it freezes the UI on a session that is
# actually waiting for the user.
_TERMINAL_WEBHOOKS = frozenset(
    {"session.status_terminated", "session.archived", "session.deleted"}
)
_STOP_REASON_WEBHOOKS = {
    "session.requires_action": "requires_action",
    "session.status_idled": "end_turn",
    "session.idled": "end_turn",
}
# A session that started running again is no longer stopped for any reason, so
# the mirror has to be able to *clear* the field, not only set it.
_RUNNING_WEBHOOKS = frozenset(
    {"session.status_run_started", "session.running", "session.status_rescheduled"}
)


def _to_response(row: dict[str, Any]) -> AgentSessionResponse:
    return AgentSessionResponse(
        id=row["id"],
        agent_id=row["agent_id"],
        title=row["title"],
        status=row["status"],
        stop_reason=row["stop_reason"],
        agent_version=row["anthropic_agent_version"],
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
    )


class AgentSessionsService:
    def __init__(
        self,
        repository: AgentsRepository | None = None,
        anthropic: AnthropicAgentsClient | None = None,
        credentials: AgentCredentialsService | None = None,
    ) -> None:
        self._repo = repository or AgentsRepository()
        self._anthropic = anthropic or AnthropicAgentsClient()
        self._credentials = credentials or AgentCredentialsService(
            repository=self._repo, anthropic=self._anthropic
        )

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def create_session(
        self, auth: AuthContext, agent_id: str, body: AgentSessionCreateRequest
    ) -> AgentSessionResponse:
        """Start a run of ``agent_id`` and say the first thing to it.

        The ordering is create → insert → send, and the middle step is not where
        it looks like it should be. Sending the first message before the insert
        would leave a *working* agent that no row references if the write then
        failed: it would be spending money, unreachable from every surface, and
        invisible to the live-session cap. Inserting first makes a failed send a
        session the user can see and retry into, which is the recoverable half of
        an unavoidable pair.

        Both of §5.4's vaults are attached here or not at all. ``sessions.update``
        documents ``vault_ids`` as "not yet supported; requests setting this field
        are rejected", so a session created without them can never be given them —
        it would run unauthorized for its whole life.
        """
        workspace_id, user_id, role = _require_dashboard_user(auth)
        agent = await self._load_agent(workspace_id, user_id, role, agent_id)

        remote_agent_id = agent["anthropic_agent_id"]
        if not remote_agent_id:
            # A draft whose sync to Anthropic failed. `update_agent` heals this on
            # the next save, so point at that rather than silently creating the
            # agent here — a session start is the wrong place to mint one.
            raise ConflictError(
                "This agent has not finished syncing to the agent runtime. "
                "Save it again, then start the session."
            )

        await self._assert_capacity(workspace_id, user_id, role)

        # ── phase 1: network, no transaction open ────────────────────────────
        vault_ids = [await self._credentials.ensure_user_vault(workspace_id, user_id)]
        if agent["ground_in_brain"]:
            vault_ids.append(
                await self._credentials.ensure_brain_credential(
                    workspace_id, user_id, role
                )
            )
        environment_id = await self._anthropic.ensure_environment()
        remote = await self._anthropic.create_session(
            agent_id=remote_agent_id,
            agent_version=agent["anthropic_agent_version"],
            environment_id=environment_id,
            vault_ids=vault_ids,
            title=body.title,
            # Correlation only, and only with ids that are already ours. It is
            # what an operator reading Anthropic's console has to work with when
            # a session misbehaves, and it carries nothing a support ticket
            # could not.
            metadata={"workspace_id": workspace_id, "user_id": user_id},
        )

        # ── phase 2: transaction ─────────────────────────────────────────────
        session_row_id = generate_id("agent_session")
        try:
            async with get_tenant_session() as session, run_in_tenant(
                session, workspace_id, user_id, role
            ) as tenant:
                row = await self._repo.insert_session(
                    tenant,
                    session_row_id=session_row_id,
                    workspace_id=workspace_id,
                    agent_id=agent_id,
                    user_id=user_id,
                    anthropic_session_id=remote["id"],
                    anthropic_agent_version=remote.get("agentVersion")
                    or agent["anthropic_agent_version"],
                    title=body.title,
                    status=remote.get("status"),
                )
                await tenant.commit()
        except Exception:
            # Name the orphan: it is a *running container*, not a dormant object
            # like the orphaned agent in `create_agent`, so it costs money until
            # somebody archives it and an operator needs the id to do that.
            log.exception(
                "session create: insert failed after Anthropic session %s started "
                "— that session is orphaned and still billing",
                remote["id"],
            )
            raise

        # ── phase 3: network again ───────────────────────────────────────────
        await self._anthropic.send_events(
            remote["id"], [_user_message(body.message)]
        )
        return _to_response(row)

    async def archive_session(self, auth: AuthContext, session_id: str) -> None:
        """Stop a session for good, and free the caller's capacity.

        Not in the plan's endpoint table and load-bearing anyway. With a live-session
        cap in front of create (see ``schemas.MAX_LIVE_SESSIONS_PER_USER``) and no
        stop button, a user whose sessions all idle without terminating would hit
        the cap and have no way out of it from any surface — the cap would be a
        one-way door. It is also the only interruption that actually ends the
        spend: ``user.interrupt`` pauses a turn, it does not end the session.
        """
        workspace_id, user_id, role = _require_dashboard_user(auth)
        row = await self._load_session(workspace_id, user_id, role, session_id)

        await self._anthropic.archive_session(row["anthropic_session_id"])

        async with get_tenant_session() as session, run_in_tenant(
            session, workspace_id, user_id, role
        ) as tenant:
            await self._repo.update_session_state(
                tenant,
                workspace_id=workspace_id,
                session_id=session_id,
                status="terminated",
                ended=True,
            )
            await tenant.commit()

    # ── reads ─────────────────────────────────────────────────────────────────

    async def list_sessions(
        self, auth: AuthContext, *, agent_id: str | None = None, limit: int = 50
    ) -> list[AgentSessionResponse]:
        workspace_id, user_id, role = _require_dashboard_user(auth)
        async with get_tenant_session() as session, run_in_tenant(
            session, workspace_id, user_id, role
        ) as tenant:
            rows = await self._repo.list_sessions(
                tenant, workspace_id=workspace_id, agent_id=agent_id, limit=limit
            )
        return [_to_response(row) for row in rows]

    async def get_session(
        self, auth: AuthContext, session_id: str
    ) -> AgentSessionResponse:
        workspace_id, user_id, role = _require_dashboard_user(auth)
        row = await self._load_session(workspace_id, user_id, role, session_id)
        return _to_response(row)

    async def list_events(
        self,
        auth: AuthContext,
        session_id: str,
        *,
        limit: int = 100,
        order: str = "asc",
        page: str | None = None,
        after: str | None = None,
    ) -> dict[str, Any]:
        """Replay proxy for reconnect. **Proxied, never stored.**

        SSE has no replay: reopening a stream starts from "now" and silently
        loses whatever happened while the client was away. §6.2's reconnect
        recipe is to open the stream *first* and then call this to fill the gap,
        deduping on event id as the live stream catches up — which is why
        ``after`` exists and why the response is forwarded with its cursor
        intact.
        """
        workspace_id, user_id, role = _require_dashboard_user(auth)
        row = await self._load_session(workspace_id, user_id, role, session_id)
        return await self._anthropic.list_events(
            row["anthropic_session_id"],
            limit=limit,
            order=order,
            page=page,
            created_at_gt=after,
        )

    async def open_stream(
        self, auth: AuthContext, session_id: str
    ) -> AsyncIterator[bytes]:
        """The live event stream, opened upstream before this returns.

        Eager on purpose — see ``stream.open_session_stream``. A lazily-opened
        stream would turn a 404 for somebody else's session into a 200 with an
        empty body, which is a much worse thing for an authorization check to
        degrade into than an error.
        """
        workspace_id, user_id, role = _require_dashboard_user(auth)
        row = await self._load_session(workspace_id, user_id, role, session_id)
        return await open_session_stream(
            self._anthropic,
            row["anthropic_session_id"],
            on_event=_observe,
        )

    # ── vendor-driven state ───────────────────────────────────────────────────

    async def sync_from_webhook(
        self, anthropic_session_id: str, *, event_type: str
    ) -> bool:
        """Fold one Anthropic webhook delivery into the mirror. Returns whether it landed.

        The delivery carries ``{id, type}`` and nothing else — no usage, no stop
        reason, no timestamps we would trust — so the type is read for what it
        means and the numbers are read back from the session itself.

        An unknown session id is not an error and is not logged as one. One
        Anthropic organization can serve more than one deployment of this app
        (staging and production against the same key is the obvious case), so a
        delivery for somebody else's session is an ordinary event, and treating
        it as a fault would make the other deployment's traffic look like an
        attack on this one.
        """
        async with get_session() as privileged:
            resolved = await self._repo.resolve_session_tenant(
                privileged, anthropic_session_id=anthropic_session_id
            )
        if resolved is None:
            return False

        workspace_id = str(resolved["workspace_id"])
        user_id = str(resolved["user_id"])

        # ── network, no transaction open ─────────────────────────────────────
        remote = await self._anthropic.get_session(anthropic_session_id)

        # ── transaction, scoped to the session's own tenant ───────────────────
        #
        # `viewer` rather than the owner's real role: nothing here needs more, and
        # the webhook has no role to inherit. RLS still requires the user id to
        # match, which is what actually scopes the write.
        async with get_tenant_session() as session, run_in_tenant(
            session, workspace_id, user_id, "viewer"
        ) as tenant:
            await self._repo.update_session_state(
                tenant,
                workspace_id=workspace_id,
                session_id=str(resolved["id"]),
                status=remote.get("status"),
                stop_reason=_STOP_REASON_WEBHOOKS.get(event_type),
                clear_stop_reason=event_type in _RUNNING_WEBHOOKS,
                input_tokens=remote.get("inputTokens"),
                output_tokens=remote.get("outputTokens"),
                ended=event_type in _TERMINAL_WEBHOOKS,
            )
            await tenant.commit()
        return True

    # ── writes ────────────────────────────────────────────────────────────────

    async def send_events(
        self, auth: AuthContext, session_id: str, body: AgentEventSendRequest
    ) -> None:
        """Send user events to a live session, in the order given."""
        workspace_id, user_id, role = _require_dashboard_user(auth)
        row = await self._load_session(workspace_id, user_id, role, session_id)
        if row["ended_at"] is not None:
            # Anthropic would reject this too, but as a 400 about a session id.
            # Saying it here means the client can render "this run has finished"
            # instead of "the request was invalid".
            raise ConflictError("This session has ended. Start a new one.")

        await self._anthropic.send_events(
            row["anthropic_session_id"], [_vendor_event(event) for event in body.events]
        )

    # ── internals ─────────────────────────────────────────────────────────────

    async def _assert_capacity(
        self, workspace_id: str, user_id: str, role: str
    ) -> None:
        """Refuse to start an nth concurrent session.

        This is the whole of our cost control, and it is not the one the plan
        specified. §5.6 asks for a per-session spend cap; Managed Agents exposes
        no budget parameter to set one with (see ``schemas.py``). A concurrency
        cap does not bound what a single session spends — nothing available to us
        does — but it does bound sessions accumulating faster than anyone
        notices, which is the failure that produces a surprising invoice.
        """
        async with get_tenant_session() as session, run_in_tenant(
            session, workspace_id, user_id, role
        ) as tenant:
            live = await self._repo.count_live_sessions(
                tenant, workspace_id=workspace_id
            )
        if live >= MAX_LIVE_SESSIONS_PER_USER:
            raise ConflictError(
                f"You already have {live} agent sessions running. End one before "
                "starting another."
            )

    async def _load_agent(
        self, workspace_id: str, user_id: str, role: str, agent_id: str
    ) -> dict[str, Any]:
        async with get_tenant_session() as session, run_in_tenant(
            session, workspace_id, user_id, role
        ) as tenant:
            row = await self._repo.get(
                tenant, workspace_id=workspace_id, agent_id=agent_id
            )
        if row is None:
            raise NotFoundError("Agent")
        return row

    async def _load_session(
        self, workspace_id: str, user_id: str, role: str, session_id: str
    ) -> dict[str, Any]:
        """One session of the caller's, or 404. The authorization check for every route."""
        async with get_tenant_session() as session, run_in_tenant(
            session, workspace_id, user_id, role
        ) as tenant:
            row = await self._repo.get_session_row(
                tenant, workspace_id=workspace_id, session_id=session_id
            )
        if row is None:
            # Somebody else's session and a nonexistent one are the same answer.
            # Telling them apart would leak that a session exists.
            raise NotFoundError("Session")
        return row


def _user_message(text: str) -> dict[str, Any]:
    return {"type": "user.message", "content": [{"type": "text", "text": text}]}


def _vendor_event(event: Any) -> dict[str, Any]:
    """Translate one validated request event into Anthropic's wire shape.

    Explicit per type rather than a ``model_dump`` passthrough. The request
    schema is ours and the wire shape is the vendor's, and letting them be the
    same object means a field added to either silently crosses to the other —
    which is how an unvalidated key ends up in a vendor payload.
    """
    if event.type == "user.message":
        return _user_message(event.text)
    if event.type == "user.interrupt":
        return {"type": "user.interrupt"}
    payload: dict[str, Any] = {
        "type": "user.tool_confirmation",
        "tool_use_id": event.tool_use_id,
        "result": event.result,
    }
    if event.deny_message:
        payload["deny_message"] = event.deny_message
    return payload


def _observe(event_type: str) -> None:
    """§4.6's metadata tap, wired to the only consumer that exists yet.

    It receives event *names* and can receive nothing else — see ``stream.py``
    for why that is structural rather than a promise. Today it logs at debug,
    which is genuinely useful (it is the only visibility into what a live session
    is doing without reading content) and is deliberately the smallest thing that
    keeps the seam exercised. When §4.6's envelope builder lands it goes here,
    and it inherits the same inability to see what the agent said.
    """
    log.debug("agent session event: %s", event_type)
