"""Service unit tests for the sources module.

Drives ``SourceService`` against fake repositories with the DB session, tenant
context, OAuth state store, and provider HTTP all stubbed. Covers the connect
state round-trip and its six callback checks, token encryption, listing,
channel scope, disconnect, and the role/duplicate/404 guards.
"""
from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from jose import jwt

from app.config.settings import settings
from app.integrations import source_oauth
from app.integrations.source_oauth import SourceOAuthError, SourceToken
from app.modules.auth.repository import AuthRepository, OAuthStateRow
from app.modules.sources import service as service_module
from app.modules.sources.repository import ChannelRow, ConnectionRow, SourceRepository
from app.modules.sources.schemas import (
    SourceCallbackRequest,
    SourceConnectRequest,
    SourceScopeRequest,
)
from app.modules.sources.service import SourceService
from app.shared.errors.app_error import (
    AppError,
    ForbiddenError,
    NotFoundError,
    UnauthorizedError,
)
from app.shared.helpers.oauth_state import decode_state
from app.shared.middleware.authenticate import AuthContext

_REDIRECT = "https://app.example/oauth/cb"


def _auth(workspace_id: str | None = "wrk_1", user_id: str = "usr_1") -> AuthContext:
    return AuthContext(
        user_id=user_id, workspace_id=workspace_id, role="admin", scopes=[], kind="jwt"
    )


def _state(
    *,
    provider: str = "slack",
    workspace_id: str = "wrk_1",
    user_id: str = "usr_1",
    redirect_uri: str = _REDIRECT,
    mode: str = "connect",
) -> str:
    token: str = jwt.encode(
        {
            "user_id": user_id,
            "workspace_id": workspace_id,
            "provider": provider,
            "redirect_uri": redirect_uri,
            "mode": mode,
            "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
        },
        settings.jwt_access_secret,
        algorithm="HS256",
    )
    return token


class _FakeRepo(SourceRepository):
    def __init__(
        self,
        *,
        connections: list[ConnectionRow] | None = None,
        channels: list[ChannelRow] | None = None,
        disconnect_ok: bool = True,
        selected_total: int = 0,
    ) -> None:
        self._connections = connections or []
        self._channels = channels or []
        self._disconnect_ok = disconnect_ok
        self._selected_total = selected_total
        self.upserted: dict[str, Any] | None = None
        self.lookback: int | None = None
        self.selections: list[tuple[str, list[str]]] = []

    async def list_connections(self, session: Any, workspace_id: str) -> list[ConnectionRow]:
        return self._connections

    async def upsert_connection(self, session: Any, **kwargs: Any) -> ConnectionRow:
        self.upserted = kwargs
        return ConnectionRow(
            id="src_new",
            provider=kwargs["provider"],
            name=kwargs["name"],
            status="connected",
            sync_status="pending",
            last_synced_at=None,
            health=None,
            active_channel_count=0,
        )

    async def disconnect(self, session: Any, workspace_id: str, source_id: str) -> bool:
        return self._disconnect_ok

    async def list_channels(
        self, session: Any, workspace_id: str, source_id: str
    ) -> list[ChannelRow]:
        return self._channels

    async def set_lookback(self, session: Any, workspace_id: str, lookback_days: int) -> None:
        self.lookback = lookback_days

    async def set_channel_selection(
        self, session: Any, workspace_id: str, provider: str, external_ids: Any
    ) -> None:
        self.selections.append((provider, list(external_ids)))

    async def selected_item_total(self, session: Any, workspace_id: str) -> int:
        return self._selected_total


class _FakeAuthRepo(AuthRepository):
    def __init__(self, *, state_row: OAuthStateRow | None = None) -> None:
        self._state_row = state_row
        self.created: dict[str, Any] | None = None
        self.consumed: str | None = None

    async def create_oauth_state(self, session: Any, **kwargs: Any) -> None:
        self.created = kwargs

    async def find_oauth_state(
        self, session: Any, state_hash: bytes, *, for_update: bool = False
    ) -> OAuthStateRow | None:
        return self._state_row

    async def mark_oauth_state_consumed(self, session: Any, state_id: str) -> None:
        self.consumed = state_id


class _FakeSession:
    async def commit(self) -> None:  # pragma: no cover - trivial
        pass


@pytest.fixture(autouse=True)
def _stub_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    @contextlib.asynccontextmanager
    async def fake_session(*_args: Any, **_kwargs: Any) -> AsyncIterator[_FakeSession]:
        yield _FakeSession()

    monkeypatch.setattr(service_module, "tenant_session", fake_session)
    monkeypatch.setattr(service_module, "get_session", fake_session)


def _row(**over: Any) -> OAuthStateRow:
    base: dict[str, Any] = {
        "id": "st_1",
        "user_id": "usr_1",
        "workspace_id": "wrk_1",
        "provider": "slack",
        "redirect_uri": _REDIRECT,
        "expires_at": datetime.now(UTC) + timedelta(minutes=5),
        "consumed_at": None,
    }
    base.update(over)
    return OAuthStateRow(**base)


def _service(
    *, repo: _FakeRepo | None = None, state_row: OAuthStateRow | None = None
) -> SourceService:
    return SourceService(
        repository=repo or _FakeRepo(),
        auth_repository=_FakeAuthRepo(state_row=state_row),
    )


def test_list_providers_returns_catalog() -> None:
    providers = SourceService().list_providers()
    assert {p.provider for p in providers} == {
        "slack",
        "notion",
        "github",
        "jira",
        "zendesk",
        "google_drive",
    }


@pytest.mark.asyncio
async def test_start_connect_builds_url_and_persists_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        source_oauth,
        "build_authorize_url",
        lambda **_kw: "https://slack.com/oauth/v2/authorize?x=1",
    )
    auth_repo = _FakeAuthRepo()
    service = SourceService(repository=_FakeRepo(), auth_repository=auth_repo)
    body = SourceConnectRequest(redirect_uri=_REDIRECT, requested_scopes=None)

    result = await service.start_connect(_auth(), "wrk_1", "slack", body)

    assert result.authorization_url.startswith("https://slack.com/")
    assert auth_repo.created is not None
    assert auth_repo.created["provider"] == "slack"
    assert auth_repo.created["redirect_uri"] == _REDIRECT
    # State is persisted as a hash, not the raw token.
    assert isinstance(auth_repo.created["state_hash"], bytes)
    # Requested/default scopes are carried in the state so the callback can
    # exchange the same scopes it authorized with.
    claims = decode_state(result.state)
    assert claims["scopes"] == ["channels:history", "channels:read"]
    assert claims["mode"] == "connect"


@pytest.mark.asyncio
async def test_start_connect_non_member_forbidden() -> None:
    service = SourceService(repository=_FakeRepo(), auth_repository=_FakeAuthRepo())
    body = SourceConnectRequest(redirect_uri=_REDIRECT, requested_scopes=None)
    with pytest.raises(ForbiddenError):
        await service.start_connect(_auth(workspace_id="wrk_OTHER"), "wrk_1", "slack", body)


@pytest.mark.asyncio
async def test_callback_success_encrypts_tokens_and_returns_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_exchange(**_kw: Any) -> SourceToken:
        return SourceToken(
            access_token="xoxb",
            refresh_token="r",
            expires_in=3600,
            external_account_id="T1",
            account_name="Riverline",
            scopes=["channels:read"],
        )

    monkeypatch.setattr(source_oauth, "exchange_code", fake_exchange)
    repo = _FakeRepo()
    service = SourceService(repository=repo, auth_repository=_FakeAuthRepo(state_row=_row()))
    body = SourceCallbackRequest(code="c", state=_state())

    source = await service.handle_callback(_auth(), "wrk_1", "slack", body)

    assert source.id == "src_new"
    assert source.provider == "slack"
    assert source.name == "Riverline"
    assert source.status == "connected"
    # Tokens reach the repository as encrypted bytes, never plaintext.
    assert repo.upserted is not None
    assert isinstance(repo.upserted["access_token_enc"], bytes)
    assert b"xoxb" not in repo.upserted["access_token_enc"]
    assert isinstance(repo.upserted["refresh_token_enc"], bytes)
    assert repo.upserted["external_account_id"] == "T1"


@pytest.mark.asyncio
async def test_callback_null_account_uses_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # GitHub/Jira return no external account id; the service must substitute a
    # non-NULL sentinel so the unique constraint / ON CONFLICT upsert works.
    async def fake_exchange(**_kw: Any) -> SourceToken:
        return SourceToken(
            access_token="ghp",
            refresh_token=None,
            expires_in=None,
            external_account_id=None,
            account_name="GitHub",
            scopes=["repo"],
        )

    monkeypatch.setattr(source_oauth, "exchange_code", fake_exchange)
    repo = _FakeRepo()
    service = SourceService(
        repository=repo, auth_repository=_FakeAuthRepo(state_row=_row(provider="github"))
    )
    body = SourceCallbackRequest(code="c", state=_state(provider="github"))

    await service.handle_callback(_auth(), "wrk_1", "github", body)

    assert repo.upserted is not None
    assert repo.upserted["external_account_id"] == "_default"
    assert repo.upserted["refresh_token_enc"] is None


@pytest.mark.asyncio
async def test_callback_wrong_mode_unauthorized() -> None:
    # A state minted for a non-connect flow must be rejected.
    service = _service(state_row=_row())
    body = SourceCallbackRequest(code="c", state=_state(mode="signin"))
    with pytest.raises(UnauthorizedError):
        await service.handle_callback(_auth(), "wrk_1", "slack", body)


@pytest.mark.asyncio
async def test_callback_bad_signature_unauthorized() -> None:
    service = SourceService(repository=_FakeRepo(), auth_repository=_FakeAuthRepo(state_row=_row()))
    body = SourceCallbackRequest(code="c", state="not-a-jwt")
    with pytest.raises(UnauthorizedError):
        await service.handle_callback(_auth(), "wrk_1", "slack", body)


@pytest.mark.asyncio
async def test_callback_provider_mismatch_unauthorized() -> None:
    # State minted for slack, but the callback path says notion.
    service = _service(state_row=_row(provider="notion"))
    body = SourceCallbackRequest(code="c", state=_state(provider="slack"))
    with pytest.raises(UnauthorizedError):
        await service.handle_callback(_auth(), "wrk_1", "notion", body)


@pytest.mark.asyncio
async def test_callback_consumed_state_unauthorized() -> None:
    service = _service(state_row=_row(consumed_at=datetime.now(UTC)))
    body = SourceCallbackRequest(code="c", state=_state())
    with pytest.raises(UnauthorizedError):
        await service.handle_callback(_auth(), "wrk_1", "slack", body)


@pytest.mark.asyncio
async def test_callback_foreign_workspace_unauthorized() -> None:
    # Authenticated for wrk_1, but the stored state belongs to wrk_2.
    service = _service(state_row=_row(workspace_id="wrk_2"))
    body = SourceCallbackRequest(code="c", state=_state(workspace_id="wrk_1"))
    with pytest.raises(UnauthorizedError):
        await service.handle_callback(_auth(), "wrk_1", "slack", body)


@pytest.mark.asyncio
async def test_callback_provider_error_maps_to_app_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(**_kw: Any) -> SourceToken:
        raise SourceOAuthError("nope", status=502, code="provider_error")

    monkeypatch.setattr(source_oauth, "exchange_code", boom)
    service = SourceService(repository=_FakeRepo(), auth_repository=_FakeAuthRepo(state_row=_row()))
    body = SourceCallbackRequest(code="c", state=_state())
    with pytest.raises(AppError) as exc:
        await service.handle_callback(_auth(), "wrk_1", "slack", body)
    assert exc.value.status == 502


@pytest.mark.asyncio
async def test_list_sources_maps_rows() -> None:
    repo = _FakeRepo(
        connections=[
            ConnectionRow(
                id="src_1",
                provider="slack",
                name="Slack",
                status="connected",
                sync_status="healthy",
                last_synced_at=None,
                health=98,
                active_channel_count=3,
            )
        ]
    )
    service = SourceService(repository=repo, auth_repository=_FakeAuthRepo())
    sources = await service.list_sources(_auth(), "wrk_1")
    assert len(sources) == 1
    dumped = sources[0].model_dump(by_alias=True)
    assert dumped["activeChannelCount"] == 3
    assert dumped["syncStatus"] == "healthy"


@pytest.mark.asyncio
async def test_list_channels_maps_rows() -> None:
    repo = _FakeRepo(
        channels=[
            ChannelRow(
                id="chn_1",
                name="#cs-escalations",
                provider="slack",
                selected=True,
                item_count=880,
            )
        ]
    )
    service = SourceService(repository=repo, auth_repository=_FakeAuthRepo())
    channels = await service.list_channels(_auth(), "wrk_1", "src_1")
    assert channels[0].item_count == 880
    assert channels[0].selected is True


@pytest.mark.asyncio
async def test_update_scope_sets_lookback_selection_and_estimate() -> None:
    repo = _FakeRepo(selected_total=120)
    service = SourceService(repository=repo, auth_repository=_FakeAuthRepo())
    body = SourceScopeRequest(
        time_range="6mo",
        channels={"slack": ["C1", "C2"], "notion": ["P1"]},
    )

    result = await service.update_scope(_auth(), "wrk_1", body)

    assert result.status == "configured"
    assert result.estimated_decisions == 12  # 120 // 10
    assert repo.lookback == 180  # 6mo
    assert ("slack", ["C1", "C2"]) in repo.selections
    assert ("notion", ["P1"]) in repo.selections


@pytest.mark.asyncio
async def test_disconnect_unknown_source_maps_to_404() -> None:
    service = _service(repo=_FakeRepo(disconnect_ok=False))
    with pytest.raises(NotFoundError):
        await service.disconnect(_auth(), "wrk_1", "src_missing")


@pytest.mark.asyncio
async def test_disconnect_success() -> None:
    service = _service(repo=_FakeRepo(disconnect_ok=True))
    await service.disconnect(_auth(), "wrk_1", "src_1")  # no raise
