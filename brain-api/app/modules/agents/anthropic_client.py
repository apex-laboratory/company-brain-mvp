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

Phases 1 and 2 are here — the agent object's CRUD and version history, plus the
vaults and credentials that hold a user's connector tokens. Sessions and
deployments arrive in phases 4 and 6; they belong in this same file when they do.

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
