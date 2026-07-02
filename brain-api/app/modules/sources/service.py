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

import secrets
from datetime import UTC, datetime, timedelta

from app.config.database import get_session
from app.config.settings import settings
from app.integrations import get_integration
from app.integrations.base import OAuthTokens
from app.jobs.queue import enqueue
from app.modules.sources.repository import SourcesRepository
from app.modules.sources.schemas import (
    AuthorizeStartOut,
    ChannelOut,
    ChannelSelectRequest,
    SourceConnectionOut,
)
from app.shared.errors.app_error import NotFoundError, UnauthorizedError, ValidationError
from app.shared.helpers.crypto import hmac_sign, hmac_verify, sha256_hash
from app.shared.helpers.ids import generate_id
from app.shared.middleware.authenticate import AuthContext
from app.shared.middleware.with_tenant import run_in_tenant


def _callback_uri(provider: str) -> str:
    return f"{settings.oauth_redirect_base_url}/api/v1/sources/{provider}/callback"


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
    def __init__(self, repository: SourcesRepository | None = None) -> None:
        self._repo = repository or SourcesRepository()

    def _require_known(self, provider: str) -> None:
        try:
            get_integration(provider)
        except KeyError:
            raise ValidationError({"provider": f"Unsupported provider: {provider}"})

    # ── OAuth start ──────────────────────────────────────────────────────────────
    async def start_authorization(self, auth: AuthContext, provider: str) -> AuthorizeStartOut:
        workspace_id, _ = _require_workspace(auth)
        self._require_known(provider)
        integration = get_integration(provider)

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
            )

        return AuthorizeStartOut(authorize_url=integration.authorize_url(state, redirect_uri))

    # ── OAuth callback ───────────────────────────────────────────────────────────
    async def handle_callback(
        self,
        provider: str,
        *,
        state: str,
        code: str | None = None,
        installation_id: str | None = None,
    ) -> str:
        """Verify state, exchange the code, persist the connection, enqueue a sync.

        ``code`` and ``installation_id`` are provider-specific: code-exchange providers
        (Notion) carry a ``code``; a GitHub App install carries an ``installation_id``.
        Returns the workspace's dashboard URL to redirect the browser to.
        """
        self._require_known(provider)

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
        integration = get_integration(provider)
        tokens = await integration.exchange_code(
            code or "", resolved.redirect_uri, installation_id=installation_id
        )

        # 4. Encrypt + persist under the resolving workspace (admin-managed table).
        connection_id = generate_id("source")
        async with get_session() as session:
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

        # 5. No sync yet: the initial backfill is the onboarding sweep
        #    (POST /sweeps after the user finishes the channel/lookback picker),
        #    so we don't ingest channels the user is about to deselect.

        # 6. Google connectors register a push channel (Drive changes.watch /
        #    Gmail users.watch) so updates arrive in real time.
        if provider in ("google_drive", "gmail"):
            await enqueue("watch_register", resolved.workspace_id, connection_id)

        return f"{settings.frontend_url}/settings/sources?connected={provider}"

    # ── connections ──────────────────────────────────────────────────────────────
    async def list_connections(self, auth: AuthContext) -> list[SourceConnectionOut]:
        workspace_id, role = _require_workspace(auth)
        async with get_session() as session:
            async with run_in_tenant(session, workspace_id, auth.user_id, role):
                rows = await self._repo.list_connections(session)
        return [SourceConnectionOut(**row) for row in rows]

    async def disconnect(self, auth: AuthContext, source_id: str) -> None:
        workspace_id, role = _require_workspace(auth)
        async with get_session() as session:
            async with run_in_tenant(session, workspace_id, auth.user_id, role):
                secrets_row = await self._repo.get_connection_secrets(session, source_id)
                if secrets_row is None:
                    raise NotFoundError("Source connection")
                integration = get_integration(secrets_row["provider"])
                # Best-effort provider-side revoke before deleting locally.
                try:
                    await integration.revoke(_dec(secrets_row["access_token_enc"]))
                except Exception:  # noqa: BLE001 — never block disconnect on a provider error
                    pass
                # Stop renewing any push channels for this source; the provider-side
                # channel then expires on its own (<= 7 days for Google watch).
                await self._repo.revoke_subscriptions_for_source(session, source_id)
                await self._repo.delete_connection(session, source_id)
                await session.commit()

    # ── channels ───────────────────────────────────────────────────────────────
    async def list_channels(self, auth: AuthContext, source_id: str) -> list[ChannelOut]:
        """Merge provider-discovered channels with persisted selection state."""
        workspace_id, role = _require_workspace(auth)
        async with get_session() as session:
            async with run_in_tenant(session, workspace_id, auth.user_id, role):
                secrets_row = await self._repo.get_connection_secrets(session, source_id)
                if secrets_row is None:
                    raise NotFoundError("Source connection")
                persisted = await self._repo.list_channels(session, source_id)

        integration = get_integration(secrets_row["provider"])
        discovered = await integration.list_channels(_dec(secrets_row["access_token_enc"]))

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
        return out

    async def select_channels(
        self, auth: AuthContext, source_id: str, req: ChannelSelectRequest
    ) -> list[ChannelOut]:
        workspace_id, role = _require_workspace(auth)
        async with get_session() as session:
            async with run_in_tenant(session, workspace_id, auth.user_id, role):
                provider = await self._repo.get_connection_provider(session, source_id)
                if provider is None:
                    raise NotFoundError("Source connection")
                if req.lookback_days is not None:
                    await self._repo.update_lookback(session, source_id, req.lookback_days)
                for ch in req.channels:
                    await self._repo.upsert_channel(
                        session,
                        channel_id=generate_id("channel"),
                        workspace_id=workspace_id,
                        source_id=source_id,
                        provider=provider,
                        external_id=ch.external_id,
                        name=ch.name,
                        selected=ch.selected,
                    )
                rows = await self._repo.list_channels(session, source_id)
                await session.commit()
        return [
            ChannelOut(
                id=r["id"],
                external_id=r["external_id"],
                name=r["name"],
                selected=bool(r["selected"]),
                item_count=int(r["item_count"]),
            )
            for r in rows
        ]


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
