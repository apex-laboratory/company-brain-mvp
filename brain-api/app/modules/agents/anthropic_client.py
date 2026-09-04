"""The single seam onto Anthropic Managed Agents (agent-builder-plan §2, §5.2).

**Nothing else in the codebase imports the Managed Agents SDK.** One seam per
vendor buys three things the plan asks for: the surface stays mockable in tests,
retry and rate-limit policy is one file's problem, and the Managed-Agents lock-in
noted in §8 is contained to a module instead of smeared across the service layer.
That lock-in is real and asymmetric — the Brain rides the swappable
``LLM_PROVIDER`` factory, agents cannot — so the boundary is worth keeping sharp.

Three rules hold for every method here:

* **Never call these inside an open transaction.** They are network I/O, and a
  pooled connection pinned during a multi-second agent create is the failure the
  two-phase pattern in ``reviews/service.py::approve`` exists to prevent
  (``BACKEND_BEST_PRACTICES.md`` §7). Callers do their Anthropic work first, then
  open a tenant session to persist the result.
* **SDK errors become ``AppError``s at this boundary.** An ``anthropic.APIError``
  reaching a router would render as a 500 with a vendor-shaped body; the
  translation table below turns the ones we can act on into typed domain errors,
  so the API contract does not leak which vendor is behind it.
* **The client is built lazily.** ``ANTHROPIC_API_KEY`` is unset in this
  deployment, and an eager client would turn a missing key into an import-time
  crash for the whole app rather than a clear error on the one route that needs
  it.

Phases 1, 2 and 4 are here — the agent object's CRUD and version history, the
vaults and credentials that hold a user's connector tokens, and the sessions and
session events those agents actually run in. Deployments arrive in phase 6; they
belong in this same file when they do.

Two phase-4 methods deliberately bypass the SDK and speak HTTP directly:
``list_events`` and ``stream_events``. Both are **proxies**, and a proxy that
parses loses — the SDK's event union is closed, so the first event type Anthropic
adds would raise or be flattened, and the frontend would lose data we had
successfully received. The vendor's base URL and auth headers still live only in
this file, so the seam is intact; it is the typing that is skipped, not the
boundary.

The credential methods carry one extra rule the agent ones do not: **a token
passed to ``create_credential`` must never be logged, echoed, or stored.** It
transits this process once, on its way from the provider's token endpoint into
Anthropic's vault, and the ``agent_credentials`` row that records the connection
deliberately has no column to put it in (migration 0028). That is the data-
liability wall in its most literal form, so the argument is not kept on ``self``,
not returned, and not included in the error we raise when the call fails.
"""
from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from app.config.settings import settings
from app.shared.errors.app_error import (
    AppError,
    ConfigurationError,
    ConflictError,
    NotFoundError,
    ValidationError,
)

log = logging.getLogger(__name__)


class AgentRuntimeError(AppError):
    """Anthropic is reachable but failed in a way the caller cannot fix.

    Distinct from the mapped 4xx errors below: those are the caller's problem
    (stale version, bad model id), this is ours or the vendor's. 502 rather than
    500 because the failure is downstream — it tells an operator reading logs
    where to look, and tells a client that a retry may succeed.
    """

    def __init__(self, message: str = "The agent runtime is unavailable.") -> None:
        super().__init__(502, "agent_runtime_unavailable", message)


# Sent on every raw request so the proxy speaks the same dialect the SDK does.
# Pinned rather than imported from the SDK: these two strings are the contract,
# and a silent bump underneath us would change which event shapes arrive.
_API_VERSION = "2023-06-01"
_AGENTS_BETA = "managed-agents-2026-04-01"


def _base_url() -> str:
    """Anthropic's API root, honouring the SDK's own override variable.

    Read at call time rather than at import: a test that points this at a local
    stub sets the variable after this module is already imported.
    """
    return (os.environ.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com").rstrip(
        "/"
    )


def _require_key() -> str:
    if not settings.anthropic_api_key:
        raise ConfigurationError(
            "ANTHROPIC_API_KEY is not configured — the agent builder runs on "
            "Anthropic Managed Agents and cannot fall back to LLM_PROVIDER. "
            "Set it in brain-api/.env."
        )
    return settings.anthropic_api_key


class AnthropicAgentsClient:
    """Thin async wrapper over ``client.beta.agents.*``.

    Stateless apart from the memoized SDK client, so the service layer can hold
    one instance for the process. Every method returns plain dicts rather than
    SDK model objects: a router that serializes an SDK type would put the
    vendor's field names into our API contract, and a test that has to build one
    is a test coupled to the SDK's constructor.
    """

    def __init__(self) -> None:
        self._client: Any = None
        self._httpx: Any = None
        # Resolved once per process, and only when ANTHROPIC_ENVIRONMENT_ID is
        # unset. See ``ensure_environment`` for why that fallback is a dev
        # convenience rather than the production answer.
        self._environment_id: str | None = None

    def _sdk(self) -> Any:
        """Build the async client on first use. See the module docstring."""
        if self._client is None:
            from anthropic import AsyncAnthropic

            self._client = AsyncAnthropic(api_key=_require_key())
        return self._client

    # ── agents ────────────────────────────────────────────────────────────────

    async def create_agent(
        self,
        *,
        name: str,
        model: str | dict[str, Any],
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        mcp_servers: list[dict[str, Any]] | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        """Create the Anthropic agent. Returns ``{"id", "version"}``.

        Called once per agent, on its first save — never per request. Anthropic's
        own guidance is explicit that ``agents.create()`` in a request path
        accumulates orphaned agent objects and defeats the versioning model, so
        the service creates lazily on first save and updates thereafter.
        """
        payload: dict[str, Any] = {"name": name, "model": model}
        # Omit rather than send None: an explicit null *clears* a field on this
        # API, which is a different intent from "not specified".
        if system is not None:
            payload["system"] = system
        if tools is not None:
            payload["tools"] = tools
        if mcp_servers is not None:
            payload["mcp_servers"] = mcp_servers
        if description is not None:
            payload["description"] = description

        agent = await self._call(lambda sdk: sdk.beta.agents.create(**payload))
        return {"id": agent.id, "version": agent.version}

    async def update_agent(
        self,
        agent_id: str,
        *,
        version: int | None = None,
        name: str | None = None,
        model: str | dict[str, Any] | None = None,
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        mcp_servers: list[dict[str, Any]] | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        """Update the agent, minting a new immutable version. Returns ``{"id", "version"}``.

        ``version`` is optimistic concurrency: supplying it makes Anthropic 409
        when it does not match the stored version, which we surface as a
        ``ConflictError`` so two people editing one agent in the dashboard get a
        "reload and retry" instead of a silent last-write-wins.

        Note the API 409s on a version mismatch **even when the fields you send
        already equal the stored values** — an unchanged save is still a
        conflict, so callers must not treat a no-op PATCH as safe to retry
        blindly.
        """
        payload: dict[str, Any] = {}
        if version is not None:
            payload["version"] = version
        for key, value in (
            ("name", name), ("model", model), ("system", system),
            ("tools", tools), ("mcp_servers", mcp_servers),
            ("description", description),
        ):
            if value is not None:
                payload[key] = value

        agent = await self._call(
            lambda sdk: sdk.beta.agents.update(agent_id, **payload)
        )
        return {"id": agent.id, "version": agent.version}

    async def get_agent(self, agent_id: str) -> dict[str, Any]:
        """Fetch the agent, primarily for its current ``version``."""
        agent = await self._call(lambda sdk: sdk.beta.agents.retrieve(agent_id))
        return {"id": agent.id, "version": agent.version, "name": agent.name}

    async def list_versions(self, agent_id: str) -> list[dict[str, Any]]:
        """The agent's append-only version history, newest first.

        Proxied rather than mirrored: version history is Anthropic's record, and
        a local copy would be one failed write away from disagreeing with it.
        """
        page = await self._call(lambda sdk: sdk.beta.agents.versions(agent_id))
        return [
            {
                "version": getattr(item, "version", None),
                "createdAt": getattr(item, "created_at", None),
                "name": getattr(item, "name", None),
            }
            for item in getattr(page, "data", [])
        ]

    async def archive_agent(self, agent_id: str) -> None:
        """Archive the agent. **Irreversible** — there is no unarchive.

        Existing sessions keep running; new ones cannot reference it. Agents have
        no delete, so this is the terminal state: the service only reaches here
        from an explicit user action, never from cleanup.
        """
        await self._call(lambda sdk: sdk.beta.agents.archive(agent_id))

    # ── vaults ────────────────────────────────────────────────────────────────

    async def create_vault(
        self, *, display_name: str, metadata: dict[str, str] | None = None
    ) -> str:
        """Create a vault and return its ``vlt_`` id.

        A vault is a credential container Anthropic injects at egress: the tokens
        inside it never enter the agent's sandbox, and Anthropic refreshes them
        itself via the ``refresh`` block on each credential. Created at most twice
        per user — once for their own connectors, once per workspace for
        ``query_brain`` (§5.4) — so this is never on a hot path.
        """
        vault = await self._call(
            lambda sdk: sdk.beta.vaults.create(
                display_name=display_name, **({"metadata": metadata} if metadata else {})
            ),
            resource="Vault",
        )
        return str(vault.id)

    # ── credentials ───────────────────────────────────────────────────────────

    async def create_mcp_oauth_credential(
        self,
        vault_id: str,
        *,
        mcp_server_url: str,
        access_token: str,
        display_name: str | None = None,
        expires_at: datetime | None = None,
        refresh: dict[str, Any] | None = None,
    ) -> str:
        """Put a user's OAuth token in the vault. Returns the ``acr``-side id.

        ``refresh`` is what makes the credential outlive its access token:
        Anthropic runs the ``refresh_token`` grant against the provider's token
        endpoint on its own, which is the whole reason we do not need a token
        store. Omitting it is correct only for tokens that genuinely do not
        expire — everything else silently dies at the first expiry and surfaces
        much later as a ``session.error`` the user reads as "the agent is broken".

        Nothing about ``access_token`` is logged. See the module docstring.
        """
        auth: dict[str, Any] = {
            "type": "mcp_oauth",
            "access_token": access_token,
            "mcp_server_url": mcp_server_url,
        }
        if expires_at is not None:
            auth["expires_at"] = expires_at
        if refresh is not None:
            auth["refresh"] = refresh

        payload: dict[str, Any] = {"auth": auth}
        if display_name is not None:
            payload["display_name"] = display_name

        credential = await self._call(
            lambda sdk: sdk.beta.vaults.credentials.create(vault_id, **payload),
            resource="Vault",
        )
        return str(credential.id)

    async def create_static_bearer_credential(
        self,
        vault_id: str,
        *,
        mcp_server_url: str,
        token: str,
        display_name: str | None = None,
    ) -> str:
        """Store a non-expiring bearer token — this is how ``query_brain`` is reached.

        The Brain's own MCP server takes an API key, not an OAuth token, and
        accepts it as ``Authorization: Bearer`` precisely so it can arrive this
        way (``app/mcp/server.py``, §4.2). It lives in the *workspace* vault, not
        each user's, so it does not eat one of their 20 credential slots (§5.4).
        """
        payload: dict[str, Any] = {
            "auth": {
                "type": "static_bearer",
                "token": token,
                "mcp_server_url": mcp_server_url,
            }
        }
        if display_name is not None:
            payload["display_name"] = display_name

        credential = await self._call(
            lambda sdk: sdk.beta.vaults.credentials.create(vault_id, **payload),
            resource="Vault",
        )
        return str(credential.id)

    async def delete_credential(self, vault_id: str, credential_id: str) -> None:
        """Remove one credential from a vault.

        Delete rather than archive: an archived credential still occupies one of
        the vault's 20 slots, so a user who disconnects and reconnects four
        providers would run out of room having connected four things. Archive is
        the right call for audit trails; a slot budget makes it the wrong one here.

        A credential Anthropic has already forgotten is not an error — the caller
        is trying to reach a state where it does not exist, and it does not.
        """
        try:
            await self._call(
                lambda sdk: sdk.beta.vaults.credentials.delete(
                    credential_id, vault_id=vault_id
                ),
                resource="Credential",
            )
        except NotFoundError:
            log.info(
                "credential %s was already absent from vault %s", credential_id, vault_id
            )

    # ── environments ──────────────────────────────────────────────────────────

    async def ensure_environment(self) -> str:
        """The ``env_`` id every session is created in, resolving it at most once.

        A session cannot be created without one: ``environment_id`` is a required
        parameter, and it names the container image and resource shape the agent's
        bash/file tools run inside. There is no implicit default.

        Resolution order is deliberate. ``ANTHROPIC_ENVIRONMENT_ID`` wins outright
        — that is the production answer, because it is the only one that survives
        a deploy with more than one process. Without it we look for an environment
        by name and create it if absent, which is right for a laptop and wrong for
        a fleet: two workers starting cold at the same moment both find nothing and
        both create one, leaving two environments with identical names. Neither is
        broken, but sessions then straddle two container configs and no row records
        which. The memo below makes that at most one race per process, not one per
        session.
        """
        if settings.anthropic_environment_id:
            return settings.anthropic_environment_id
        if self._environment_id is not None:
            return self._environment_id

        name = settings.anthropic_environment_name
        page = await self._call(
            lambda sdk: sdk.beta.environments.list(limit=100), resource="Environment"
        )
        for item in getattr(page, "data", []):
            if getattr(item, "name", None) == name and not getattr(
                item, "archived_at", None
            ):
                self._environment_id = str(item.id)
                return self._environment_id

        created = await self._call(
            lambda sdk: sdk.beta.environments.create(
                name=name,
                description="Container config for Brainite agent-builder sessions.",
            ),
            resource="Environment",
        )
        log.info("created Anthropic environment %s (%r)", created.id, name)
        self._environment_id = str(created.id)
        return self._environment_id

    # ── sessions ──────────────────────────────────────────────────────────────

    async def create_session(
        self,
        *,
        agent_id: str,
        environment_id: str,
        agent_version: int | None = None,
        vault_ids: list[str] | None = None,
        title: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Start a session against ``agent_id``. Returns the mirrored fields.

        ``agent_version`` pins the version for the session's whole life. Passing
        the bare id instead would pin *the latest at create time*, which sounds
        equivalent and is not: an agent edited while the session is idle would
        change what its next turn runs, and the run record would then describe a
        configuration that never actually executed.

        ``vault_ids`` is the only chance to attach credentials — the update
        endpoint documents the field as "not yet supported; requests setting this
        field are rejected", so a session created without a vault can never be
        given one. Both vaults go on here (§5.4) or the agent runs unauthorized.
        """
        agent: dict[str, Any] | str = (
            {"id": agent_id, "version": agent_version}
            if agent_version is not None
            else agent_id
        )
        payload: dict[str, Any] = {"agent": agent, "environment_id": environment_id}
        if vault_ids:
            payload["vault_ids"] = vault_ids
        if title is not None:
            payload["title"] = title
        if metadata:
            payload["metadata"] = metadata

        session = await self._call(
            lambda sdk: sdk.beta.sessions.create(**payload), resource="Session"
        )
        return _session_view(session)

    async def get_session(self, session_id: str) -> dict[str, Any]:
        """Fetch one session's status and cumulative usage.

        This is what the webhook is for. A delivery carries only the session id
        and a type — no usage, no stop reason — so the numbers still have to be
        read back, and this is the read.
        """
        session = await self._call(
            lambda sdk: sdk.beta.sessions.retrieve(session_id), resource="Session"
        )
        return _session_view(session)

    async def archive_session(self, session_id: str) -> None:
        """Archive a session. Terminal, and the only way to stop one costing money.

        An already-archived session is not an error: the caller wants a state
        where it is stopped, and it is.
        """
        try:
            await self._call(
                lambda sdk: sdk.beta.sessions.archive(session_id), resource="Session"
            )
        except NotFoundError:
            log.info("session %s was already gone at archive", session_id)

    # ── session events ────────────────────────────────────────────────────────

    async def send_events(
        self, session_id: str, events: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Send user events (message, interrupt, tool confirmation) to a session.

        Typed through the SDK rather than the raw proxy below, because this is the
        one direction where a malformed body is *our* bug: the events come from
        our own schemas, and a 400 here should be caught in review, not rendered.
        """
        sent = await self._call(
            lambda sdk: sdk.beta.sessions.events.send(session_id, events=events),
            resource="Session",
        )
        return {
            "events": [
                item.to_dict() if hasattr(item, "to_dict") else item
                for item in getattr(sent, "events", [])
            ]
        }

    async def list_events(
        self,
        session_id: str,
        *,
        limit: int = 100,
        order: str = "asc",
        page: str | None = None,
        types: list[str] | None = None,
        created_at_gt: str | None = None,
    ) -> dict[str, Any]:
        """Replay a session's events. **Proxied verbatim, never stored.**

        Deliberately *not* routed through the SDK's typed models. This is a proxy,
        and a proxy that parses loses: the event union is a closed set in the
        installed SDK, so the first event type Anthropic adds would either raise
        or be silently flattened, and the frontend — which only has to render what
        it recognises and ignore the rest — would lose data we successfully
        received. Raw JSON forwards the unknown intact.
        """
        params: dict[str, Any] = {"limit": limit, "order": order, "beta": "true"}
        if page:
            params["page"] = page
        if types:
            params["types"] = types
        if created_at_gt:
            params["created_at_gt"] = created_at_gt
        return await self._raw_get(f"/v1/sessions/{session_id}/events", params)

    @asynccontextmanager
    async def stream_events(
        self, session_id: str, *, event_deltas: list[str] | None = None
    ) -> AsyncIterator[Any]:
        """Open the session's SSE stream and yield the live ``httpx.Response``.

        The caller reads lines off it and forwards them; nothing here buffers,
        parses or retains a single frame (§5.5). Returning the response rather
        than an iterator keeps the connection's lifetime tied to the ``async
        with``, so a client that disconnects mid-stream closes the upstream socket
        instead of leaving Anthropic writing into a dropped consumer.

        ``event_deltas`` opts into incremental previews for the named event types.
        The delta type is ``content_delta`` — *not* the Messages API's
        ``content_block_delta`` — and the buffered event still arrives afterwards
        and stays authoritative.
        """
        params: list[tuple[str, str]] = [("beta", "true")]
        params += [("event_deltas[]", kind) for kind in event_deltas or []]

        client = self._http()
        async with client.stream(
            "GET",
            f"{_base_url()}/v1/sessions/{session_id}/events/stream",
            params=params,
            headers=self._vendor_headers(),
            timeout=_stream_timeout(),
        ) as response:
            if response.status_code >= 400:
                # The body has not been read yet on a streaming response, and the
                # error mapper needs it; read it before deciding, then raise.
                await response.aread()
                _raise_for_status(response, resource="Session")
            yield response

    # ── raw vendor proxy ──────────────────────────────────────────────────────
    #
    # Two operations bypass the SDK on purpose: the events list and the event
    # stream. Both are *proxies*, and both would be made worse by typing —
    # see ``list_events``. Everything else goes through the SDK, so the vendor's
    # base URL and auth headers still live in exactly this one file.

    def _http(self) -> Any:
        import httpx

        if self._httpx is None:
            self._httpx = httpx.AsyncClient()
        return self._httpx

    def _vendor_headers(self) -> dict[str, str]:
        return {
            "x-api-key": _require_key(),
            "anthropic-version": _API_VERSION,
            "anthropic-beta": _AGENTS_BETA,
            "accept": "application/json",
        }

    async def _raw_get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        import httpx

        try:
            response = await self._http().get(
                f"{_base_url()}{path}",
                params=params,
                headers=self._vendor_headers(),
                timeout=_request_timeout(),
            )
        except httpx.HTTPError as exc:
            log.exception("anthropic raw GET %s could not connect", path)
            raise AgentRuntimeError(
                "Could not reach the agent runtime. Try again shortly."
            ) from exc
        _raise_for_status(response, resource="Session")
        body = response.json()
        return body if isinstance(body, dict) else {"data": body}

    async def aclose(self) -> None:
        """Close the proxy's HTTP client. Called from the app's shutdown hook."""
        if self._httpx is not None:
            await self._httpx.aclose()
            self._httpx = None

    # ── error translation ─────────────────────────────────────────────────────

    async def _call(self, fn: Any, *, resource: str = "Agent") -> Any:
        """Run one SDK coroutine, mapping vendor errors onto domain errors.

        The chain is most-specific-first. A bare ``except APIError`` would erase
        the distinction between "your version is stale" (the caller can fix it)
        and "Anthropic is down" (they cannot), which is exactly the distinction
        the dashboard needs to decide between showing a reload prompt and a retry.

        ``resource`` names the noun a 404 is about. It is a parameter rather than
        a constant because a vault call that reported "Agent not found." would
        send an operator looking at the wrong object entirely.
        """
        import anthropic

        try:
            return await fn(self._sdk())
        except anthropic.NotFoundError as exc:
            # NotFoundError renders "{resource} not found." — pass the noun only.
            raise NotFoundError(resource) from exc
        except anthropic.ConflictError as exc:
            raise ConflictError(
                "This agent was changed by someone else. Reload and re-apply "
                "your edit."
            ) from exc
        except anthropic.BadRequestError as exc:
            # ValidationError's message is fixed ("Request is invalid."); the
            # vendor's text goes in ``details``, where the envelope surfaces it.
            # It names the offending field, which is more use to the caller than
            # anything we could synthesize.
            raise ValidationError({"agentRuntime": str(exc)}) from exc
        except anthropic.AuthenticationError as exc:
            raise ConfigurationError(
                "ANTHROPIC_API_KEY was rejected by Anthropic."
            ) from exc
        except anthropic.PermissionDeniedError as exc:
            raise ConfigurationError(
                "This ANTHROPIC_API_KEY lacks access to Managed Agents."
            ) from exc
        except anthropic.APIStatusError as exc:
            log.exception("anthropic agents call failed with %s", exc.status_code)
            raise AgentRuntimeError(
                "The agent runtime is unavailable. Try again shortly."
            ) from exc
        except anthropic.APIConnectionError as exc:
            log.exception("anthropic agents call could not connect")
            raise AgentRuntimeError(
                "Could not reach the agent runtime. Try again shortly."
            ) from exc


# ── module helpers ────────────────────────────────────────────────────────────

def _request_timeout() -> Any:
    """Budget for an ordinary request/response call against the raw proxy."""
    import httpx

    return httpx.Timeout(30.0, connect=10.0)


def _stream_timeout() -> Any:
    """Budget for the event stream: connect only, **no read timeout**.

    The stream stays open for the length of an agent's turn, which is minutes of
    silence between frames while a tool runs. A read timeout would sever a
    perfectly healthy stream mid-thought, and the client would read that
    disconnect as the agent having stopped.
    """
    import httpx

    return httpx.Timeout(None, connect=10.0)


def _session_view(session: Any) -> dict[str, Any]:
    """Flatten one SDK session object into the fields ``agent_sessions`` mirrors.

    Only the pointer-and-counter fields cross this boundary. There is no branch
    here that could pick up message content, and that is the point: the mirror
    is what migration 0028 has columns for, and it has columns for nothing else.
    """
    usage = getattr(session, "usage", None)
    agent = getattr(session, "agent", None)
    return {
        "id": str(session.id),
        "status": getattr(session, "status", None),
        "title": getattr(session, "title", None),
        "agentVersion": getattr(agent, "version", None) if agent else None,
        "environmentId": getattr(session, "environment_id", None),
        "vaultIds": list(getattr(session, "vault_ids", []) or []),
        "inputTokens": getattr(usage, "input_tokens", None) if usage else None,
        "outputTokens": getattr(usage, "output_tokens", None) if usage else None,
        "createdAt": getattr(session, "created_at", None),
        "updatedAt": getattr(session, "updated_at", None),
        "archivedAt": getattr(session, "archived_at", None),
    }


def _raise_for_status(response: Any, *, resource: str) -> None:
    """Map a raw proxy response's status onto the same domain errors ``_call`` uses.

    Kept in step with ``_call`` by hand rather than shared, because the two have
    genuinely different inputs — an SDK exception class versus an integer — and
    a shared abstraction over those would be longer than both. The status codes
    below are the ones ``_call`` distinguishes; anything else is downstream.
    """
    status = response.status_code
    if status < 400:
        return
    if status == 404:
        raise NotFoundError(resource)
    if status == 409:
        raise ConflictError(
            "This session was changed by someone else. Reload and retry."
        )
    if status == 400:
        raise ValidationError({"agentRuntime": _vendor_message(response)})
    if status == 401:
        raise ConfigurationError("ANTHROPIC_API_KEY was rejected by Anthropic.")
    if status == 403:
        raise ConfigurationError("This ANTHROPIC_API_KEY lacks access to Managed Agents.")
    log.error("anthropic raw call failed with %s", status)
    raise AgentRuntimeError("The agent runtime is unavailable. Try again shortly.")


def _vendor_message(response: Any) -> str:
    """The vendor's own explanation of a 400, truncated, or a flat fallback.

    Only reached on a 400, where the body names the offending field and is more
    use to the caller than anything we could synthesize. Truncated because it is
    going into a response envelope, and an unbounded vendor string there is a
    log-injection and payload-size problem at once.
    """
    try:
        body = response.json()
    except Exception:  # noqa: BLE001 — a non-JSON error body is still an error
        return "The agent runtime rejected the request."
    detail = body.get("error", {}).get("message") if isinstance(body, dict) else None
    return str(detail or "The agent runtime rejected the request.")[:500]
