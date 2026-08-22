"""Sources business logic (BACKEND_BEST_PRACTICES.md §2 layering).

Orchestrates the OAuth lifecycle across three collaborators: the provider
integration (consent URL, code exchange, fetch), the repository (persistence),
and crypto (token encryption at rest). Tenant-scoped writes run inside
``run_in_tenant`` so RLS (admin-only on ``source_connections``) applies.

OAuth ``state`` is a signed, single-use nonce: ``"{nonce}.{hmac(nonce)}"`` signed
with ``JWT_ACCESS_SECRET`` (§7). The SHA-256 of the full state is stored in
``oauth_states``; the callback verifies the signature, then atomically consumes
the row (race-safe single use).
"""
from __future__ import annotations

import logging
import re
import secrets
from datetime import UTC, datetime, timedelta

from app.config.database import get_session, get_tenant_session
from app.config.settings import settings
from app.integrations import get_integration
from app.integrations.base import OAuthTokens
from app.integrations.google_common import GOOGLE_PUSH_PROVIDERS
from app.jobs.queue import enqueue
from app.modules.sources.repository import SourcesRepository
from app.modules.sources.schemas import (
    AuthorizeStartOut,
    ChannelOut,
    ChannelSelectRequest,
    DiscardGroupOut,
    SourceConnectionOut,
    SourceScopeOut,
    SourceReportOut,
)
from app.modules.sweeps.schemas import SweepOut
from app.modules.sweeps.service import SweepsService
from app.shared.errors.app_error import (
    ConfigurationError,
    NotFoundError,
    UnauthorizedError,
    ValidationError,
)
from app.shared.helpers.crypto import hmac_sign, hmac_verify, sha256_hash
from app.shared.helpers.ids import generate_id
from app.shared.middleware.authenticate import AuthContext
from app.shared.middleware.with_tenant import run_in_tenant


log = logging.getLogger(__name__)


def _callback_uri(provider: str) -> str:
    return f"{settings.oauth_redirect_base_url}/api/v1/sources/{provider}/callback"


# Providers whose OAuth + API are scoped to a customer subdomain ({sub}.zendesk.com).
_SUBDOMAIN_PROVIDERS = frozenset({"zendesk"})
# DNS label rules — strict, because the value becomes a URL host in authorize_url.
# This is the guard against an attacker steering the OAuth redirect off-host.
_SUBDOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,61}[a-z0-9]$")


# Where the callback lands the browser. ``returnTo`` ends up in a Location header,
# so free-form validation is not enough — only exact allowlist matches are stored
# (which trivially rules out schemes, hosts, '//' and backslashes). Non-allowlisted
# values degrade to the default rather than 422 so a stale frontend build still
# completes the connect.
_RETURN_TO_ALLOWLIST = frozenset({"/onboarding", "/dashboard/sources"})
_DEFAULT_RETURN_TO = "/settings/sources"
# The one destination whose flow fires its own sweep afterwards; every other
# connect auto-starts a scoped import in the callback (see handle_callback step 5).
_ONBOARDING_RETURN_TO = "/onboarding"


def _resolve_return_to(return_to: str | None) -> str | None:
    return return_to if return_to in _RETURN_TO_ALLOWLIST else None


def _match_frontend_origin(origin: str | None) -> str | None:
    """Origin header vs. the FRONTEND_URLS allowlist; None degrades to the default."""
    allowed = settings.frontend_urls or [settings.frontend_url]
    return origin if origin in allowed else None


def _validate_subdomain(provider: str, subdomain: str | None) -> str | None:
    """Validate + normalize the subdomain for subdomain-scoped providers.

    Returns the normalized subdomain (or ``None`` for providers that don't use one).
    Raises ``ValidationError`` if a subdomain-scoped provider is missing/invalid.
    """
    if provider not in _SUBDOMAIN_PROVIDERS:
        return None
    normalized = (subdomain or "").strip().lower()
    if not _SUBDOMAIN_RE.match(normalized):
        raise ValidationError(
            {"subdomain": f"A valid {provider} subdomain is required (e.g. 'acme')."}
        )
    return normalized


# The credentials each provider needs configured before an OAuth flow can start.
# Missing any → a 501 (provider_not_configured) instead of bouncing the user to the
# provider with an empty client_id and orphaning an oauth_states row per attempt.
_REQUIRED_CREDENTIALS: dict[str, tuple[str, ...]] = {
    "notion": ("notion_client_id", "notion_client_secret"),
    "slack": ("slack_client_id", "slack_client_secret"),
    "zendesk": ("zendesk_client_id", "zendesk_client_secret"),
    "jira": ("jira_client_id", "jira_client_secret"),
    "gmail": ("google_client_id", "google_client_secret"),
    "google_drive": ("google_client_id", "google_client_secret"),
    "github": (
        "github_app_id",
        "github_app_private_key",
        "github_app_slug",
        "github_app_client_id",
        "github_app_client_secret",
    ),
}


def _require_configured(provider: str) -> None:
    """Raise ``ConfigurationError`` if the provider's OAuth credentials aren't all set."""
    missing = [
        name for name in _REQUIRED_CREDENTIALS.get(provider, ()) if not getattr(settings, name, "")
    ]
    if missing:
        raise ConfigurationError(
            f"The {provider} connector is not configured on this deployment."
        )


# The four pipeline stages that can discard an event (app/pipeline/orchestrator.py),
# in the user's language. Mapped here rather than in the frontend so the pipeline's
# internal vocabulary stays out of the UI, and so a stage added later renders as its
# raw name (see _stage_label) instead of a blank row.
_DISCARD_STAGE_LABELS = {
    "normalize": "No readable content",
    "relevance_gate": "Not durable knowledge",
    "decision_identifier": "No decision found",
    "skill_extractor": "Extractor abstained",
}


def _stage_label(stage: str | None) -> str:
    """Human label for a discard stage; unknown stages degrade to the raw name."""
    if not stage:
        return "Unknown"
    return _DISCARD_STAGE_LABELS.get(stage, stage)


def _require_workspace(auth: AuthContext) -> tuple[str, str]:
    """Narrow auth to a workspace-scoped context, asserting both fields are set.

    All connector routes are protected by ``require_role("admin")``, which rejects
    any token whose role is None (rank 0) before the service is reached. This
    assertion makes that invariant explicit and satisfies the type checker so
    ``run_in_tenant`` callers pass ``str``, not ``str | None``.
    """
    if auth.workspace_id is None or auth.role is None:
        raise RuntimeError(
            "BUG: connector service called without workspace context — "
            "ensure require_role is declared on this route"
        )
    return auth.workspace_id, auth.role


class SourcesService:
    def __init__(
        self,
        repository: SourcesRepository | None = None,
        sweeps_service: SweepsService | None = None,
    ) -> None:
        self._repo = repository or SourcesRepository()
        # A per-source import is a scoped sweep, so the sweep service owns it —
        # idempotency, scoping, and the enqueue all stay in one place.
        self._sweeps = sweeps_service or SweepsService()

    def _require_known(self, provider: str) -> None:
        try:
            get_integration(provider)
        except KeyError:
            raise ValidationError({"provider": f"Unsupported provider: {provider}"})

    # ── OAuth start ──────────────────────────────────────────────────────────────
    async def start_authorization(
        self,
        auth: AuthContext,
        provider: str,
        subdomain: str | None = None,
        return_to: str | None = None,
        origin: str | None = None,
    ) -> AuthorizeStartOut:
        workspace_id, _ = _require_workspace(auth)
        self._require_known(provider)
        _require_configured(provider)
        integration = get_integration(provider)

        # Subdomain-scoped providers (Zendesk) need a validated subdomain up front:
        # it is the URL host of the consent page and is stored for the callback.
        subdomain = _validate_subdomain(provider, subdomain)
        config = {"subdomain": subdomain} if subdomain else None

        # Bound to the state row (never the provider redirect_uri, which must stay
        # byte-identical to what's registered with each provider).
        return_to = _resolve_return_to(return_to)
        frontend_origin = _match_frontend_origin(origin)

        nonce = secrets.token_urlsafe(32)
        state = f"{nonce}.{hmac_sign(nonce, settings.jwt_access_secret)}"
        redirect_uri = _callback_uri(provider)
        expires_at = datetime.now(UTC) + timedelta(seconds=settings.oauth_state_ttl_seconds)

        async with get_session() as session:
            await self._repo.create_oauth_state(
                session,
                state_hash=sha256_hash(state),
                provider=provider,
                redirect_uri=redirect_uri,
                user_id=auth.user_id,
                workspace_id=workspace_id,
                expires_at=expires_at,
                subdomain=subdomain,
                return_to=return_to,
                frontend_origin=frontend_origin,
            )

        return AuthorizeStartOut(
            authorize_url=integration.authorize_url(state, redirect_uri, config=config)
        )

    # ── OAuth callback ───────────────────────────────────────────────────────────
    async def handle_callback(
        self,
        provider: str,
        *,
        state: str,
        code: str | None = None,
        installation_id: str | None = None,
        error: str | None = None,
    ) -> str:
        """Verify state, exchange the code, persist the connection, enqueue a sync.

        ``code`` and ``installation_id`` are provider-specific: code-exchange providers
        (Notion) carry a ``code``; a GitHub App install carries an ``installation_id``.
        ``error`` is a provider-signalled failure (the user declined consent).
        Returns the workspace's dashboard URL to redirect the browser to.
        """
        self._require_known(provider)

        # 0. The user declined the consent screen (or the provider errored). There is no
        # code to exchange — redirect back with the error and leave the single-use state
        # untouched (unconsumed) so no spurious "state already used" appears if the
        # browser retries. The stored return_to is still honored (a decline mid-onboarding
        # must land back on onboarding), via a read-only peek gated on the stateless
        # signature check: a forged state gets the default without touching the DB.
        if error:
            peeked = None
            nonce, _, signature = state.partition(".")
            if signature and hmac_verify(nonce, signature, settings.jwt_access_secret):
                async with get_session() as session:
                    peeked = await self._repo.peek_oauth_state(
                        session,
                        state_hash=sha256_hash(state),
                        provider=provider,
                        now=datetime.now(UTC),
                    )
            return_to = peeked.return_to if peeked else None
            frontend_origin = peeked.frontend_origin if peeked else None
            return (
                f"{frontend_origin or settings.frontend_url}"
                f"{return_to or _DEFAULT_RETURN_TO}?error={provider}"
            )

        # 1. Stateless signature check before any DB work.
        nonce, _, signature = state.partition(".")
        if not signature or not hmac_verify(nonce, signature, settings.jwt_access_secret):
            raise UnauthorizedError("Invalid OAuth state.")

        # 2. Atomically consume the stored state (single use, unexpired, provider match).
        async with get_session() as session:
            resolved = await self._repo.consume_oauth_state(
                session,
                state_hash=sha256_hash(state),
                provider=provider,
                now=datetime.now(UTC),
            )
        if resolved is None:
            raise UnauthorizedError("OAuth state is expired, already used, or unknown.")

        # 3. Exchange the code/installation for tokens (uses the redirect_uri bound to
        # the state). ``code`` may be None for installation-based providers (GitHub App).
        # ``installation_id`` is the generic provider-specific carrier. The stored,
        # server-validated subdomain (Zendesk) MUST take precedence over the callback's
        # query ``installation_id``: for subdomain-scoped providers that value becomes a
        # URL host, and the query param is attacker-influenceable and unvalidated, so
        # trusting it could redirect the token POST (with the client secret) off-host.
        # GitHub has no stored subdomain, so it falls through to its query install id.
        integration = get_integration(provider)
        tokens = await integration.exchange_code(
            code or "",
            resolved.redirect_uri,
            installation_id=resolved.subdomain or installation_id,
        )

        # 4. Encrypt + persist under the resolving workspace (admin-managed table).
        connection_id = generate_id("source")
        async with get_tenant_session() as session:
            async with run_in_tenant(session, resolved.workspace_id, resolved.user_id, "admin"):
                connection_id = await self._repo.upsert_connection(
                    session,
                    connection_id=connection_id,
                    workspace_id=resolved.workspace_id,
                    provider=provider,
                    name=_connection_name(provider, tokens),
                    access_token_enc=_enc(tokens.access_token),
                    refresh_token_enc=_enc(tokens.refresh_token),
                    token_expires_at=tokens.expires_at,
                    scopes=tokens.scopes,
                    external_account_id=tokens.external_account_id,
                    connected_by=resolved.user_id,
                )
                await session.commit()

        # 5. Onboarding defers the backfill to the sweep the wizard fires after the
        #    channel/lookback picker, so we don't ingest channels the user is about to
        #    deselect. A connect from anywhere else has no such follow-up step: the
        #    user lands back on Sources and nothing would ever fetch this source's
        #    history (webhooks only carry events from now on, and webhook_ingest never
        #    advances the cursor). So start a scoped import for it here.
        #
        #    For channel-scoped providers this reads every channel at the default
        #    lookback, since a dashboard connect has no picker before the redirect.
        #    That is the deliberate trade: over-ingesting is recoverable (skills land
        #    in the review queue, and narrowing scope governs later syncs), whereas a
        #    user who never opens Manage would otherwise get no history at all.
        if resolved.return_to != _ONBOARDING_RETURN_TO:
            await self._start_backfill_unattended(
                resolved.workspace_id, resolved.user_id, connection_id
            )

        # 6. Google connectors register a push channel (Drive changes.watch /
        #    Gmail users.watch) so updates arrive in real time.
        if provider in GOOGLE_PUSH_PROVIDERS:
            await enqueue("watch_register", resolved.workspace_id, connection_id)

        return (
            f"{resolved.frontend_origin or settings.frontend_url}"
            f"{resolved.return_to or _DEFAULT_RETURN_TO}?connected={provider}"
        )

    # ── connections ──────────────────────────────────────────────────────────────
    async def list_connections(self, auth: AuthContext) -> list[SourceConnectionOut]:
        workspace_id, role = _require_workspace(auth)
        async with get_tenant_session() as session:
            async with run_in_tenant(session, workspace_id, auth.user_id, role):
                rows = await self._repo.list_connections(session)
        return [SourceConnectionOut(**row) for row in rows]

    # ── read report ──────────────────────────────────────────────────────────────
    async def get_report(self, auth: AuthContext, source_id: str) -> SourceReportOut:
        """What this source has read, what became knowledge, and why the rest didn't.

        Exists because "we read 56 things and kept none" is information the pipeline
        already records per event (``source_events.pipeline_meta``) and nothing
        surfaced — leaving a successful import that produced no skills looking
        identical to a broken one.
        """
        workspace_id, role = _require_workspace(auth)
        async with get_tenant_session() as session:
            async with run_in_tenant(session, workspace_id, auth.user_id, role):
                status = await self._repo.get_connection_status(session, source_id)
                if status is None:
                    raise NotFoundError("Source connection")
                totals = await self._repo.connection_event_totals(session, source_id)
                groups = await self._repo.discard_breakdown(session, source_id)

        return SourceReportOut(
            source_id=source_id,
            items_read=totals["items_read"],
            skills_kept=totals["skills_kept"],
            discarded=totals["discarded"],
            pending_items=totals["pending_items"],
            discarded_by_stage=[
                DiscardGroupOut(
                    stage=g["stage"] or "unknown",
                    label=_stage_label(g["stage"]),
                    count=g["count"],
                )
                for g in groups
            ],
        )

    # ── historical backfill ──────────────────────────────────────────────────────
    async def _start_backfill_unattended(
        self, workspace_id: str, user_id: str, source_id: str
    ) -> None:
        """Kick off a scoped import from the OAuth callback (no request identity).

        The callback is a provider redirect carrying no JWT, so it authenticates via
        the signed state and there is no ``AuthContext`` to pass down. The state's
        resolved workspace/user *is* the identity — the same pair step 4 just wrote
        the connection under — so it is reconstructed here as the admin context the
        rest of the callback already runs as.

        Best-effort by design: a failed import must not turn a successful OAuth
        connect into an error page. The connection stays ``needs_backfill``, so the
        Sources page still offers the import.
        """
        auth = AuthContext(user_id=user_id, workspace_id=workspace_id, role="admin")
        try:
            await self._sweeps.start(auth, source_ids=[source_id])
        except Exception:  # noqa: BLE001 — never fail the connect on the import
            log.exception("auto-backfill failed for %s; left for manual import", source_id)

    async def start_backfill(
        self, auth: AuthContext, source_id: str
    ) -> tuple[SweepOut, bool]:
        """Import one connection's history. Returns ``(sweep, created)``.

        Connecting a source ingests nothing (see ``handle_callback`` step 5), so this
        is what actually gives a connection its past. It runs the ordinary sweep
        machinery scoped to a single connection — same job, same progress shape, same
        failure isolation — rather than a parallel code path.

        Idempotent by way of ``SweepsService.start``: a sweep already covering this
        source is returned instead of stacking a second import over it.
        """
        workspace_id, role = _require_workspace(auth)
        async with get_tenant_session() as session:
            async with run_in_tenant(session, workspace_id, auth.user_id, role):
                status = await self._repo.get_connection_status(session, source_id)
        if status is None:
            raise NotFoundError("Source connection")
        if status != "connected":
            raise ValidationError(
                {"source_id": f"This source is {status} — reconnect it before importing."}
            )
        return await self._sweeps.start(auth, source_ids=[source_id])

    async def _uninstall_if_last(self, secrets_row: dict, source_id: str) -> None:
        """Remove the provider-side installation, if this was the last one using it.

        Only providers whose installation outlives their tokens declare ``uninstall``
        (today: GitHub) — the optional-capability convention used by ``push_delivery``
        and friends, so nothing is required of the other connectors.

        The count runs on the **privileged** pool on purpose. "Does another workspace
        still hold this account?" is a cross-tenant question that RLS would answer
        `no` by construction, which would uninstall a GitHub App out from under every
        other workspace connected to it the first time any one of them disconnected.

        Best-effort like ``revoke``: the local disconnect must succeed regardless, and
        a leftover installation is a visible, user-fixable state — whereas failing the
        disconnect would leave a connection the user has explicitly asked to remove.
        """
        integration = get_integration(secrets_row["provider"])
        uninstall = getattr(integration, "uninstall", None)
        account_id = secrets_row.get("external_account_id")
        if uninstall is None or not account_id:
            return
        try:
            async with get_session() as session:
                others = await self._repo.count_other_connections_for_account(
                    session,
                    provider=secrets_row["provider"],
                    external_account_id=account_id,
                    excluding_source_id=source_id,
                )
            if others:
                log.info(
                    "%s account %s still connected in %d other workspace(s) — "
                    "leaving the installation in place",
                    secrets_row["provider"], account_id, others,
                )
                return
            await uninstall(account_id)
        except Exception:  # noqa: BLE001 — never block disconnect on a provider error
            log.exception(
                "provider-side uninstall failed for %s; the installation may need "
                "removing manually", source_id,
            )

    async def disconnect(self, auth: AuthContext, source_id: str) -> None:
        workspace_id, role = _require_workspace(auth)
        # txn A: read-only secrets lookup, then release the connection — the
        # provider revoke below can take the full HTTP timeout, and holding a
        # pooled RLS connection across it starves the tenant pool.
        async with get_tenant_session() as session:
            async with run_in_tenant(session, workspace_id, auth.user_id, role):
                secrets_row = await self._repo.get_connection_secrets(session, source_id)
        if secrets_row is None:
            raise NotFoundError("Source connection")
        integration = get_integration(secrets_row["provider"])
        # Best-effort provider-side revoke before deleting locally (no txn open).
        try:
            await integration.revoke(_dec(secrets_row["access_token_enc"]))
        except Exception:  # noqa: BLE001 — never block disconnect on a provider error
            pass
        await self._uninstall_if_last(secrets_row, source_id)
        # txn B: stop renewing any push channels for this source (the provider-side
        # channel then expires on its own, <= 7 days for Google watch) and delete.
        async with get_tenant_session() as session:
            async with run_in_tenant(session, workspace_id, auth.user_id, role):
                await self._repo.revoke_subscriptions_for_source(session, source_id)
                await self._repo.delete_connection(session, source_id)
                await session.commit()

    # ── channels ───────────────────────────────────────────────────────────────
    async def _resolve_access_token(
        self, auth: AuthContext, secrets_row: dict
    ) -> str:
        """Decrypt the connection's access token, refreshing it if near expiry.

        The channel picker can be opened long after the last sync (GitHub installation
        and Jira access tokens expire in ~1h). Without this, ``list_channels`` would call
        the provider with a dead token and 500. Mirrors the jobs layer's ``_resolve_token``:
        a refreshed token is persisted so it isn't re-refreshed on the next open.

        The provider refresh runs with **no transaction open**; only the persist of
        a refreshed token opens a (short) tenant session of its own.
        """
        access_token = _dec(secrets_row["access_token_enc"])
        expires = secrets_row.get("token_expires_at")
        refresh_enc = secrets_row.get("refresh_token_enc")
        if expires and expires < datetime.now(UTC) + timedelta(minutes=5) and refresh_enc:
            integration = get_integration(secrets_row["provider"])
            refreshed = await integration.refresh(_dec(refresh_enc))  # network — no txn
            access_token = refreshed.access_token
            workspace_id, role = _require_workspace(auth)
            async with get_tenant_session() as session:
                async with run_in_tenant(session, workspace_id, auth.user_id, role):
                    await self._repo.update_tokens(
                        session,
                        secrets_row["id"],
                        access_token_enc=_enc(refreshed.access_token),  # type: ignore[arg-type]
                        token_expires_at=refreshed.expires_at,
                        refresh_token_enc=(
                            _enc(refreshed.refresh_token) if refreshed.refresh_token else None
                        ),
                    )
                    await session.commit()
        return access_token

    async def list_channels(self, auth: AuthContext, source_id: str) -> SourceScopeOut:
        """Merge provider-discovered channels with persisted selection state."""
        workspace_id, role = _require_workspace(auth)
        async with get_tenant_session() as session:
            async with run_in_tenant(session, workspace_id, auth.user_id, role):
                secrets_row = await self._repo.get_connection_secrets(session, source_id)
                if secrets_row is None:
                    raise NotFoundError("Source connection")
                persisted = await self._repo.list_channels(session, source_id)

        # Provider I/O (token refresh + discovery) with no pooled connection held.
        access_token = await self._resolve_access_token(auth, secrets_row)
        integration = get_integration(secrets_row["provider"])
        discovered = await integration.list_channels(access_token)

        by_external = {p["external_id"]: p for p in persisted}
        out: list[ChannelOut] = []
        for ch in discovered:
            row = by_external.get(ch.external_id)
            out.append(
                ChannelOut(
                    id=row["id"] if row else None,
                    external_id=ch.external_id,
                    name=ch.name,
                    selected=bool(row["selected"]) if row else False,
                    item_count=int(row["item_count"]) if row else 0,
                )
            )
        return SourceScopeOut(channels=out, lookback_days=int(secrets_row["lookback_days"]))

    async def select_channels(
        self, auth: AuthContext, source_id: str, req: ChannelSelectRequest
    ) -> SourceScopeOut:
        workspace_id, role = _require_workspace(auth)
        async with get_tenant_session() as session:
            async with run_in_tenant(session, workspace_id, auth.user_id, role):
                scope = await self._repo.get_connection_scope(session, source_id)
                if scope is None:
                    raise NotFoundError("Source connection")
                provider = scope["provider"]
                lookback_days = int(scope["lookback_days"])
                if req.lookback_days is not None:
                    updated = await self._repo.update_lookback(
                        session, source_id, req.lookback_days
                    )
                    lookback_days = int(updated) if updated is not None else lookback_days
                await self._repo.upsert_channels(
                    session,
                    [
                        {
                            "id": generate_id("channel"),
                            "workspace_id": workspace_id,
                            "source_id": source_id,
                            "provider": provider,
                            "external_id": ch.external_id,
                            "name": ch.name,
                            "selected": ch.selected,
                        }
                        for ch in req.channels
                    ],
                )
                rows = await self._repo.list_channels(session, source_id)
                await session.commit()
        return SourceScopeOut(
            channels=[
                ChannelOut(
                    id=r["id"],
                    external_id=r["external_id"],
                    name=r["name"],
                    selected=bool(r["selected"]),
                    item_count=int(r["item_count"]),
                )
                for r in rows
            ],
            lookback_days=lookback_days,
        )


def _connection_name(provider: str, tokens: OAuthTokens) -> str:
    return tokens.raw.get("workspace_name") or provider.capitalize()


def _enc(plaintext: str | None) -> bytes | None:
    """Encrypt to the BYTEA column shape (AES-GCM base64 string → bytes)."""
    from app.shared.helpers.crypto import encrypt

    return encrypt(plaintext).encode() if plaintext is not None else None


def _dec(blob: bytes | None) -> str:
    from app.shared.helpers.crypto import decrypt

    if blob is None:
        raise ValidationError({"token": "Connection has no stored access token."})
    return decrypt(blob.decode())
