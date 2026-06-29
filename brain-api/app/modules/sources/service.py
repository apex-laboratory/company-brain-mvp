"""Source-integration business logic (BEST_PRACTICES §2, §7, §8, §12).

Framework-agnostic orchestration of the read-only source connectors: build a
signed OAuth state, exchange the provider code, store encrypted tokens, and
manage channel scope. Connecting/managing a source touches encrypted credentials,
so connect, callback, list, scope, and disconnect are admin-only (router
``require_role("admin")`` + ``connections_admin`` RLS). Listing channels is open
to any member (``channels_select`` RLS).

OAuth state reuses the shared ``oauth_states`` table via :class:`AuthRepository`
(the table was built for both login and source flows — migration 0004), so the
single-use, signed-state CSRF protection isn't reimplemented here. Provider
round-trips never happen while a DB connection/lock is held: the state is
validated and consumed in one short transaction, then the code is exchanged.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.config.database import get_session
from app.config.settings import settings
from app.integrations import source_oauth
from app.integrations.source_oauth import SourceOAuthError
from app.modules.auth.repository import AuthRepository
from app.modules.sources.repository import LOOKBACK_DAYS, ConnectionRow, SourceRepository
from app.modules.sources.schemas import (
    ProviderOut,
    SourceCallbackRequest,
    SourceChannelOut,
    SourceConnectRequest,
    SourceConnectStartOut,
    SourceOut,
    SourceScopeRequest,
    SourceScopeUpdatedOut,
)
from app.shared.errors.app_error import AppError, NotFoundError, UnauthorizedError
from app.shared.helpers.crypto import encrypt, sha256_hash
from app.shared.helpers.ids import generate_id
from app.shared.helpers.oauth_state import decode_state, encode_state
from app.shared.logger import get_logger
from app.shared.middleware.authenticate import AuthContext
from app.shared.middleware.authorize import assert_workspace_member
from app.shared.middleware.with_tenant import tenant_session

log = get_logger()

# Rough decisions-per-item yield used to preview a sweep before ingestion runs.
# Real counts replace this once the sync job populates source_channels.item_count.
_ITEMS_PER_DECISION = 10

# State mode for the source-connection OAuth flow; verified on callback so a
# state minted for a different flow can't be replayed here.
_CONNECT_MODE = "connect"

# Stand-in account key for providers that return no external account id (GitHub,
# Jira). Keeping the column non-NULL lets the (workspace_id, provider,
# external_account_id) unique constraint enforce one connection per provider and
# makes the upsert's ON CONFLICT deterministic instead of racing on NULLs.
_DEFAULT_ACCOUNT = "_default"


class SourceService:
    def __init__(
        self,
        repository: SourceRepository | None = None,
        auth_repository: AuthRepository | None = None,
    ) -> None:
        self._repository = repository or SourceRepository()
        self._auth_repository = auth_repository or AuthRepository()

    # ── catalog ────────────────────────────────────────────────────────────────
    @staticmethod
    def list_providers() -> list[ProviderOut]:
        return [ProviderOut(**entry) for entry in source_oauth.provider_catalog()]

    # ── connect (start) ─────────────────────────────────────────────────────────
    async def start_connect(
        self,
        auth: AuthContext,
        workspace_id: str,
        provider: str,
        body: SourceConnectRequest,
    ) -> SourceConnectStartOut:
        workspace_id = assert_workspace_member(auth, workspace_id)
        scopes = body.requested_scopes or source_oauth.default_scopes(provider)

        expires_at = datetime.now(UTC) + timedelta(
            seconds=settings.oauth_state_ttl_seconds
        )
        # Carry the requested scopes in the state so the callback exchanges the
        # *same* scopes that were authorized (not just the provider defaults).
        state_raw = encode_state(
            user_id=auth.user_id,
            workspace_id=workspace_id,
            provider=provider,
            redirect_uri=body.redirect_uri,
            mode=_CONNECT_MODE,
            expires_at=expires_at,
            scopes=scopes,
        )

        # Build first: an unconfigured provider raises 501 before we persist state.
        authorization_url = source_oauth.build_authorize_url(
            provider=provider,
            redirect_uri=body.redirect_uri,
            state=state_raw,
            scopes=scopes,
        )

        async with get_session() as session:
            await self._auth_repository.create_oauth_state(
                session,
                state_hash=sha256_hash(state_raw),
                provider=provider,
                redirect_uri=body.redirect_uri,
                expires_at=expires_at,
                user_id=auth.user_id,
                workspace_id=workspace_id,
            )
            await session.commit()

        return SourceConnectStartOut(authorization_url=authorization_url, state=state_raw)

    # ── connect (callback) ───────────────────────────────────────────────────────
    async def handle_callback(
        self,
        auth: AuthContext,
        workspace_id: str,
        provider: str,
        body: SourceCallbackRequest,
    ) -> SourceOut:
        workspace_id = assert_workspace_member(auth, workspace_id)

        # Checks 1 & 2: signature valid + not expired (shared helper).
        claims = decode_state(body.state)
        state_hash = sha256_hash(body.state)

        # Phase 1: validate + consume the state in a short transaction (no provider
        # round-trip while the row lock / connection is held).
        async with get_session() as session:
            row = await self._auth_repository.find_oauth_state(
                session, state_hash, for_update=True
            )
            # Checks 3–8: exists, single-use, mode/provider/redirect/workspace/user.
            if (
                row is None
                or row.consumed_at is not None
                or claims.get("mode") != _CONNECT_MODE
                or claims.get("provider") != provider
                or claims.get("redirect_uri") != row.redirect_uri
                or row.workspace_id != workspace_id
                or row.user_id != auth.user_id
            ):
                raise UnauthorizedError("Invalid OAuth state")
            redirect_uri = row.redirect_uri
            await self._auth_repository.mark_oauth_state_consumed(session, row.id)
            await session.commit()

        # Phase 2: provider round-trip (no DB connection held).
        try:
            token = await source_oauth.exchange_code(
                provider=provider,
                code=body.code,
                redirect_uri=redirect_uri,
                scopes=claims.get("scopes") or source_oauth.default_scopes(provider),
            )
        except SourceOAuthError as exc:
            log.warning("source_oauth_failed", provider=provider, error_code=exc.code)
            raise AppError(exc.status, exc.code, exc.message) from exc

        token_expires_at = (
            datetime.now(UTC) + timedelta(seconds=token.expires_in)
            if token.expires_in is not None
            else None
        )

        # Phase 3: persist encrypted tokens under the admin tenant context.
        async with tenant_session(auth, workspace_id) as session:
            connection = await self._repository.upsert_connection(
                session,
                new_id=generate_id("source"),
                workspace_id=workspace_id,
                provider=provider,
                name=token.account_name,
                access_token_enc=_seal_required(token.access_token),
                refresh_token_enc=_seal(token.refresh_token),
                token_expires_at=token_expires_at,
                scopes=token.scopes,
                external_account_id=token.external_account_id or _DEFAULT_ACCOUNT,
                connected_by=auth.user_id,
            )
            await session.commit()

        log.info("source_connected", workspace_id=workspace_id, provider=provider)
        return _to_source_out(connection)

    # ── list ─────────────────────────────────────────────────────────────────────
    async def list_sources(
        self, auth: AuthContext, workspace_id: str
    ) -> list[SourceOut]:
        workspace_id = assert_workspace_member(auth, workspace_id)
        async with tenant_session(auth, workspace_id) as session:
            rows = await self._repository.list_connections(session, workspace_id)
        return [_to_source_out(r) for r in rows]

    async def list_channels(
        self, auth: AuthContext, workspace_id: str, source_id: str
    ) -> list[SourceChannelOut]:
        workspace_id = assert_workspace_member(auth, workspace_id)
        async with tenant_session(auth, workspace_id) as session:
            rows = await self._repository.list_channels(session, workspace_id, source_id)
        return [
            SourceChannelOut(
                id=r.id,
                name=r.name,
                provider=r.provider,  # DB source_provider enum ⊆ SourceProvider literal
                selected=r.selected,
                item_count=r.item_count,
            )
            for r in rows
        ]

    # ── scope ──────────────────────────────────────────────────────────────────
    async def update_scope(
        self, auth: AuthContext, workspace_id: str, body: SourceScopeRequest
    ) -> SourceScopeUpdatedOut:
        workspace_id = assert_workspace_member(auth, workspace_id)
        async with tenant_session(auth, workspace_id) as session:
            await self._repository.set_lookback(
                session, workspace_id, LOOKBACK_DAYS[body.time_range]
            )
            for provider, external_ids in body.channels.items():
                await self._repository.set_channel_selection(
                    session, workspace_id, provider, external_ids
                )
            total = await self._repository.selected_item_total(session, workspace_id)
            await session.commit()

        return SourceScopeUpdatedOut(
            status="configured",
            estimated_decisions=total // _ITEMS_PER_DECISION,
        )

    # ── disconnect ───────────────────────────────────────────────────────────────
    async def disconnect(
        self, auth: AuthContext, workspace_id: str, source_id: str
    ) -> None:
        workspace_id = assert_workspace_member(auth, workspace_id)
        async with tenant_session(auth, workspace_id) as session:
            removed = await self._repository.disconnect(session, workspace_id, source_id)
            await session.commit()
        if not removed:
            raise NotFoundError("Source")


# ── module-level helpers ────────────────────────────────────────────────────────
def _seal_required(value: str) -> bytes:
    """AES-GCM encrypt a token into bytes for the bytea column."""
    return encrypt(value).encode("utf-8")


def _seal(value: str | None) -> bytes | None:
    """Encrypt an optional token (e.g. a refresh token a provider may omit)."""
    return _seal_required(value) if value is not None else None


def _to_source_out(c: ConnectionRow) -> SourceOut:
    return SourceOut(
        id=c.id,
        provider=c.provider,  # DB source_provider enum ⊆ SourceProvider literal
        name=c.name,
        status=c.status,
        sync_status=c.sync_status,
        last_synced_at=c.last_synced_at,
        health=c.health,
        active_channel_count=c.active_channel_count,
    )
