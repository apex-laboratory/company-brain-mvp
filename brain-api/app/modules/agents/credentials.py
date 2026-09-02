"""Per-connector OAuth → vault (agent-builder-plan §5.2, §5.3, phase 2).

**A token reaching this module is on its way out of it.** The consent flow ends
with an access token in memory; it is POSTed to an Anthropic vault and dropped.
Nothing writes it to Postgres, no field on ``agent_credentials`` could hold it,
and no log line in this file interpolates a grant. That is the data-liability
wall (§1) at its narrowest point — the one moment connector credentials exist on
our infrastructure at all — so the code path is written to be read for that
property first and its features second.

The ordering that follows from it is **vendor first, then store**, the same rule
``service.py`` states for agents, and here it is not merely a transaction-hygiene
rule (§7) but the reason the promise holds: the credential must already be at
Anthropic before we have anything worth recording, because what we record is a
pointer to it.

Two shapes are worth knowing before reading:

* **Find-or-create vault, not create-vault.** A vault is per ``(workspace, user)``
  and holds up to 20 credentials, so connecting a second provider must land in
  the first vault. Concurrent connects race; the partial unique index settles it
  and the loser re-reads (``insert_vault``).
* **Re-connecting replaces.** A vault keys credentials uniquely by MCP server
  URL, so re-authorizing GitHub is a delete-then-create against the same slot,
  not a second credential. Skipping the delete would leave the old credential
  occupying one of the user's 20 slots forever.

The workspace vault of §5.4 is created here too (``ensure_workspace_vault``) but
is deliberately left *empty*: the ``query_brain`` static-bearer credential that
belongs in it needs an API key minted under an admin's authority, and which
caller does that — and when — is a phase-4 session-lifecycle decision, not a
phase-2 one. The vault half is what phase 2 owes; the seam is marked below.
"""
from __future__ import annotations

import logging
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from app.config.database import get_session, get_tenant_session
from app.config.settings import settings
from app.modules.agents import oauth
from app.modules.agents.anthropic_client import AnthropicAgentsClient
from app.modules.agents.catalog import ConnectorSpec, get_connector, list_connectors
from app.modules.agents.repository import AgentsRepository
from app.modules.agents.schemas import (
    AgentAuthorizeStartResponse,
    AgentCredentialResponse,
    AgentCredentialsOverview,
    ConnectorCatalogEntry,
    UnauthorizedConnector,
)
from app.modules.agents.service import _require_dashboard_user
from app.shared.errors.app_error import (
    NotFoundError,
    UnauthorizedError,
    ValidationError,
)
from app.shared.helpers.crypto import hmac_sign, hmac_verify, sha256_hash
from app.shared.helpers.ids import generate_id
from app.shared.middleware.authenticate import AuthContext
from app.shared.middleware.with_tenant import run_in_tenant

log = logging.getLogger(__name__)

_DEFAULT_RETURN_TO = "/dashboard/agents"

# Where the callback may land the browser. An exact allowlist is what
# ``sources`` uses, and it cannot work here: the useful destination is
# ``/dashboard/agents/{id}/edit`` and the id is minted per agent. So the shape is
# allowlisted instead — a fixed prefix plus path segments drawn from an alphabet
# with no ``/``, ``\``, ``:``, ``?``, ``#`` or ``.`` in it. That rules out a
# scheme, a host, a protocol-relative ``//``, a query string and a fragment,
# which is the whole attack surface of a value that ends up in a ``Location``
# header. Anything else degrades to the default rather than 422, so a stale
# frontend build can still finish a connect.
_RETURN_TO_RE = re.compile(r"^/dashboard/agents(?:/[A-Za-z0-9_-]{1,64})*$")

# RLS on the tables this flow writes (``agent_vaults``, ``agent_credentials``)
# is scoped by workspace *and* user, never by role — a vault is personal, and
# there is no role that grants access to somebody else's. The callback carries no
# ``AuthContext`` to read a role from, so it passes the least-privilege one; the
# policies do not consult it.
_CALLBACK_ROLE = "viewer"


def _callback_uri(provider: str) -> str:
    return (
        f"{settings.oauth_redirect_base_url}"
        f"/api/v1/agent-credentials/{provider}/callback"
    )


def _resolve_return_to(return_to: str | None) -> str | None:
    if return_to and _RETURN_TO_RE.match(return_to):
        return return_to
    return None


def _match_frontend_origin(origin: str | None) -> str | None:
    """Origin header vs. the FRONTEND_URLS allowlist; ``None`` takes the default."""
    allowed = settings.frontend_urls or [settings.frontend_url]
    return origin if origin in allowed else None


def _require_spec(provider: str) -> ConnectorSpec:
    spec = get_connector(provider)
    if spec is None:
        # Unknown *and* not-offered-on-this-deployment both land here. A 422
        # naming the field is the right answer to both: the caller sent a
        # provider this API does not serve, and which of the two reasons applies
        # is a deployment detail they cannot act on differently.
        raise ValidationError({"provider": f"Unsupported connector: {provider}"})
    return spec


class AgentCredentialsService:
    def __init__(
        self,
        repository: AgentsRepository | None = None,
        anthropic: AnthropicAgentsClient | None = None,
    ) -> None:
        self._repo = repository or AgentsRepository()
        self._anthropic = anthropic or AnthropicAgentsClient()

    # ── reads ─────────────────────────────────────────────────────────────────

    async def list_catalog(self, auth: AuthContext) -> list[ConnectorCatalogEntry]:
        """The curated catalog, annotated with what this user has already connected.

        The annotation is why this is not a static file the frontend could fetch
        itself: ``connected`` is per-caller, and a picker that had to make a
        second request for it would render every connected provider as "Connect"
        for one frame.
        """
        workspace_id, user_id, role = _require_dashboard_user(auth)
        async with get_tenant_session() as session, run_in_tenant(
            session, workspace_id, user_id, role
        ) as tenant:
            rows = await self._repo.list_credentials(
                tenant, workspace_id=workspace_id, user_id=user_id
            )
        connected = {row["mcp_server_url"] for row in rows}
        return [
            ConnectorCatalogEntry(
                provider=spec.provider,
                display_name=spec.display_name,
                description=spec.description,
                mcp_server_url=spec.mcp_server_url,
                docs_url=spec.docs_url,
                scopes=spec.scopes,
                configured=spec.configured,
                connected=spec.mcp_server_url in connected,
            )
            for spec in list_connectors()
        ]

    async def overview(self, auth: AuthContext) -> AgentCredentialsOverview:
        """What the caller has connected, and which agents want something they lack."""
        workspace_id, user_id, role = _require_dashboard_user(auth)
        async with get_tenant_session() as session, run_in_tenant(
            session, workspace_id, user_id, role
        ) as tenant:
            connections = await self._repo.list_credentials(
                tenant, workspace_id=workspace_id, user_id=user_id
            )
            missing = await self._repo.list_unauthorized_connectors(
                tenant, workspace_id=workspace_id, user_id=user_id
            )
        return AgentCredentialsOverview(
            connections=[_to_credential(row) for row in connections],
            needs_authorization=[
                UnauthorizedConnector(
                    agent_id=row["agent_id"],
                    agent_name=row["agent_name"],
                    connector_id=row["connector_id"],
                    connector_name=row["connector_name"],
                    provider=row["provider"],
                    mcp_server_url=row["mcp_server_url"],
                )
                for row in missing
            ],
        )

    # ── OAuth start ───────────────────────────────────────────────────────────

    async def start_authorization(
        self,
        auth: AuthContext,
        provider: str,
        *,
        return_to: str | None = None,
        origin: str | None = None,
    ) -> AgentAuthorizeStartResponse:
        """Mint a single-use state and return the provider's consent URL.

        The state is ``"{nonce}.{hmac(nonce)}"`` and only its SHA-256 is stored,
        exactly as the ingestion flow does it — the signature makes a forged state
        rejectable without a database round-trip, and the stored hash makes a real
        one single-use. The PKCE verifier is derived from the same nonce
        (``oauth.code_verifier``), so nothing extra needs storing or expiring.
        """
        workspace_id, user_id, _ = _require_dashboard_user(auth)
        spec = _require_spec(provider)

        nonce = secrets.token_urlsafe(32)
        state = f"{nonce}.{hmac_sign(nonce, settings.jwt_access_secret)}"
        redirect_uri = _callback_uri(spec.provider)
        expires_at = datetime.now(UTC) + timedelta(
            seconds=settings.oauth_state_ttl_seconds
        )

        # Build the URL *before* writing the state: ``authorize_url`` is what
        # raises when the deployment has no OAuth client for this provider, and
        # an unconfigured provider should not leave a state row behind per attempt.
        url = oauth.authorize_url(
            spec, state=state, redirect_uri=redirect_uri, nonce=nonce
        )

        async with get_session() as session:
            await self._repo.create_agent_oauth_state(
                session,
                state_hash=sha256_hash(state),
                provider=spec.provider,
                redirect_uri=redirect_uri,
                user_id=user_id,
                workspace_id=workspace_id,
                expires_at=expires_at,
                return_to=_resolve_return_to(return_to),
                frontend_origin=_match_frontend_origin(origin),
            )

        return AgentAuthorizeStartResponse(authorize_url=url)

    # ── OAuth callback ────────────────────────────────────────────────────────

    async def handle_callback(
        self,
        provider: str,
        *,
        state: str,
        code: str | None = None,
        error: str | None = None,
    ) -> str:
        """Exchange, vault, record, redirect. Returns the URL to bounce the browser to.

        Read the step numbers as the ordering guarantee: every network call
        happens with no transaction open, and the credential is at Anthropic
        before any row claims it exists.
        """
        spec = _require_spec(provider)

        # 0. The user declined consent. There is nothing to exchange, so the
        #    single-use state is left *unconsumed* — a browser retry must not meet
        #    a spurious "state already used". The stored destination is still
        #    honoured, via a read-only peek gated on the stateless signature check
        #    so a forged state takes the default without touching the database.
        if error:
            peeked = None
            nonce, _, signature = state.partition(".")
            if signature and hmac_verify(nonce, signature, settings.jwt_access_secret):
                async with get_session() as session:
                    peeked = await self._repo.peek_agent_oauth_state(
                        session,
                        state_hash=sha256_hash(state),
                        provider=spec.provider,
                        now=datetime.now(UTC),
                    )
            return _redirect(peeked, f"?error={spec.provider}")

        # 1. Stateless signature check before any database work.
        nonce, _, signature = state.partition(".")
        if not signature or not hmac_verify(nonce, signature, settings.jwt_access_secret):
            raise UnauthorizedError("Invalid OAuth state.")

        # 2. Atomically consume it (single use, unexpired, this provider only).
        async with get_session() as session:
            resolved = await self._repo.consume_agent_oauth_state(
                session,
                state_hash=sha256_hash(state),
                provider=spec.provider,
                now=datetime.now(UTC),
            )
        if resolved is None:
            raise UnauthorizedError("OAuth state is expired, already used, or unknown.")

        workspace_id: str = resolved["workspace_id"]
        user_id: str = resolved["user_id"]

        # 3. Network: trade the code for tokens. Held in memory from here to
        #    step 6 and nowhere else, ever.
        grant = await oauth.exchange_code(
            spec, code=code or "", redirect_uri=resolved["redirect_uri"], nonce=nonce
        )

        # 4. Find-or-create this user's vault.
        vault_id = await self._ensure_vault(
            workspace_id, user_id, acting_user_id=user_id
        )

        # 5. Free the slot if this provider was already connected. A vault keys
        #    credentials by MCP server URL, so a re-connect is a replace — and an
        #    orphaned old credential would eat one of the user's 20 slots.
        existing = await self._existing_credential(
            workspace_id, user_id, spec.mcp_server_url
        )
        if existing is not None:
            await self._anthropic.delete_credential(
                vault_id, existing["anthropic_credential_id"]
            )

        # 6. Network: the token leaves this process. After this line the only
        #    thing we hold is an id.
        anthropic_credential_id = await self._anthropic.create_mcp_oauth_credential(
            vault_id,
            mcp_server_url=spec.mcp_server_url,
            access_token=grant.access_token,
            display_name=grant.account_label or spec.display_name,
            expires_at=grant.expires_at,
            refresh=oauth.refresh_block(spec, grant),
        )
        if grant.refresh_token is None:
            # Worth a line in the log, not an error: some providers issue
            # non-expiring tokens and this is correct for them. For the rest it
            # is the quiet start of a credential that will stop working, and the
            # fix is a scope change in the catalog, not a code change (§8).
            log.info(
                "connector %s issued no refresh token for user %s; the vault "
                "credential cannot be refreshed",
                spec.provider, user_id,
            )

        # 7. Transaction: record the pointer.
        credential_id = generate_id("agent_credential")
        try:
            async with get_tenant_session() as session, run_in_tenant(
                session, workspace_id, user_id, _CALLBACK_ROLE
            ) as tenant:
                await self._repo.upsert_credential(
                    tenant,
                    credential_id=existing["id"] if existing else credential_id,
                    workspace_id=workspace_id,
                    user_id=user_id,
                    provider=spec.provider,
                    mcp_server_url=spec.mcp_server_url,
                    anthropic_credential_id=anthropic_credential_id,
                    display_name=grant.account_label or spec.display_name,
                )
                await tenant.commit()
        except Exception:
            # The mirror image of the orphaned-agent case in ``service.py``: the
            # credential exists in the vault and no row points at it. Name it, so
            # an operator reading logs can find and delete it — it is occupying a
            # slot the user cannot see or reclaim.
            log.exception(
                "agent credential: storing the pointer failed after credential %s "
                "was created in vault %s — that credential is now orphaned",
                anthropic_credential_id, vault_id,
            )
            raise

        return _redirect(resolved, f"?connected={spec.provider}")

    # ── disconnect ────────────────────────────────────────────────────────────

    async def disconnect(self, auth: AuthContext, credential_id: str) -> None:
        """Revoke a connection: delete it from the vault, then forget the pointer.

        Vendor first here too, and for a sharper reason than ordering hygiene: if
        the row went first and the vault call then failed, the credential would
        keep working for every agent while the user was told it was gone. A vault
        holds 20 credentials, so a leaked slot is also a resource the user cannot
        reclaim through any surface.
        """
        workspace_id, user_id, role = _require_dashboard_user(auth)

        async with get_tenant_session() as session, run_in_tenant(
            session, workspace_id, user_id, role
        ) as tenant:
            row = await self._repo.get_credential(
                tenant,
                workspace_id=workspace_id,
                user_id=user_id,
                credential_id=credential_id,
            )
            vault = await self._repo.get_vault(
                tenant, workspace_id=workspace_id, user_id=user_id
            )
        if row is None:
            raise NotFoundError("Connection")

        if vault is not None:
            # A missing vault row with a live credential row should not happen,
            # but deleting the pointer is still the right outcome: there is
            # nothing left we could reach to revoke.
            await self._anthropic.delete_credential(
                vault["anthropic_vault_id"], row["anthropic_credential_id"]
            )

        async with get_tenant_session() as session, run_in_tenant(
            session, workspace_id, user_id, role
        ) as tenant:
            await self._repo.delete_credential_row(
                tenant,
                workspace_id=workspace_id,
                user_id=user_id,
                credential_id=credential_id,
            )
            await tenant.commit()

    # ── vaults ────────────────────────────────────────────────────────────────

    async def ensure_user_vault(self, workspace_id: str, user_id: str) -> str:
        """This user's vault id at Anthropic, creating it if this is their first.

        Public because phase 4 needs it: a session attaches ``[user_vault,
        workspace_vault]``, and the user vault must exist by then even for a user
        whose agent uses only Brain grounding.
        """
        return await self._ensure_vault(workspace_id, user_id, acting_user_id=user_id)

    async def ensure_workspace_vault(self, workspace_id: str, user_id: str) -> str:
        """The workspace's shared vault id, creating it if absent (§5.4).

        Its whole purpose is to hold the single ``query_brain`` static-bearer
        credential so that credential does not eat one of every user's 20 slots,
        and so rotating the Brain's key is one write instead of N.

        **The vault is created empty.** Putting ``query_brain``'s credential in it
        means minting an API key with the ``brain:query`` scope, and ``api_keys``
        is admin-only at the RLS layer while the caller who first needs grounding
        may be any member. Who mints it, under whose authority, and whether it is
        provisioned eagerly or on first session is a session-lifecycle decision —
        phase 4's, where the caller is known. This is the seam it plugs into.
        """
        return await self._ensure_vault(workspace_id, None, acting_user_id=user_id)

    async def _ensure_vault(
        self, workspace_id: str, owner_user_id: str | None, *, acting_user_id: str
    ) -> str:
        """Find-or-create, with the concurrent-connect race resolved by the index.

        ``owner_user_id`` of ``None`` addresses the workspace vault; it is the
        value written to the row. ``acting_user_id`` is who the transaction runs
        as, and the two differ for exactly that case — a workspace vault is
        nobody's, but somebody still has to be the caller for RLS to evaluate.

        Read, then create outside the transaction, then insert — and if the
        insert conflicts, somebody else won the race and their vault is the one
        to use.
        """
        row = await self._read_vault(workspace_id, owner_user_id, acting_user_id)
        if row is not None:
            return str(row["anthropic_vault_id"])

        # ── network, no transaction open ─────────────────────────────────────
        metadata = {"workspace_id": workspace_id}
        if owner_user_id is not None:
            metadata["user_id"] = owner_user_id
        anthropic_vault_id = await self._anthropic.create_vault(
            display_name=(
                f"Brainite workspace {workspace_id}"
                if owner_user_id is None
                else f"Brainite user {owner_user_id}"
            ),
            metadata=metadata,
        )

        # ── transaction ──────────────────────────────────────────────────────
        async with get_tenant_session() as session, run_in_tenant(
            session, workspace_id, acting_user_id, _CALLBACK_ROLE
        ) as tenant:
            inserted = await self._repo.insert_vault(
                tenant,
                vault_id=generate_id("agent_vault"),
                workspace_id=workspace_id,
                user_id=owner_user_id,
                anthropic_vault_id=anthropic_vault_id,
            )
            await tenant.commit()

        if inserted is not None:
            return str(inserted["anthropic_vault_id"])

        # Lost the race. The vault we just created is orphaned — logged rather
        # than deleted, because deleting on a mis-read would destroy the winner's
        # credentials, and an empty vault costs nothing.
        log.warning(
            "vault create raced for workspace=%s user=%s; Anthropic vault %s is orphaned",
            workspace_id, owner_user_id, anthropic_vault_id,
        )
        winner = await self._read_vault(workspace_id, owner_user_id, acting_user_id)
        if winner is None:
            # The row is neither insertable nor readable: the only way here is an
            # RLS mismatch, which would make every later read fail the same way.
            raise NotFoundError("Vault")
        return str(winner["anthropic_vault_id"])

    async def _read_vault(
        self, workspace_id: str, owner_user_id: str | None, acting_user_id: str
    ) -> dict[str, Any] | None:
        async with get_tenant_session() as session, run_in_tenant(
            session, workspace_id, acting_user_id, _CALLBACK_ROLE
        ) as tenant:
            return await self._repo.get_vault(
                tenant, workspace_id=workspace_id, user_id=owner_user_id
            )

    async def _existing_credential(
        self, workspace_id: str, user_id: str, mcp_server_url: str
    ) -> dict[str, Any] | None:
        async with get_tenant_session() as session, run_in_tenant(
            session, workspace_id, user_id, _CALLBACK_ROLE
        ) as tenant:
            return await self._repo.get_credential_for_server(
                tenant,
                workspace_id=workspace_id,
                user_id=user_id,
                mcp_server_url=mcp_server_url,
            )


def _redirect(resolved: dict[str, Any] | None, query: str) -> str:
    origin = (resolved or {}).get("frontend_origin") or settings.frontend_url
    path = (resolved or {}).get("return_to") or _DEFAULT_RETURN_TO
    return f"{origin}{path}{query}"


def _to_credential(row: dict[str, Any]) -> AgentCredentialResponse:
    return AgentCredentialResponse(
        id=str(row["id"]),
        provider=str(row["provider"]),
        display_name=row["display_name"],
        mcp_server_url=str(row["mcp_server_url"]),
        connected_at=row["connected_at"],
    )
