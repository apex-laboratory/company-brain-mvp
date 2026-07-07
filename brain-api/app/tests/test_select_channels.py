"""Channel selection + lookback persistence tests (KAN-2).

``PATCH /sources/{id}/channels`` is onboarding's picker: it upserts the channel
selection and, when ``lookback_days`` is present, persists the "how far back?"
window that bounds the connection's first sync.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from app.modules.sources import service as service_module
from app.modules.sources.schemas import ChannelSelection, ChannelSelectRequest
from app.modules.sources.service import SourcesService
from app.shared.middleware.authenticate import AuthContext


class _AsyncCtx:
    def __init__(self, value: object) -> None:
        self._value = value

    async def __aenter__(self) -> object:
        return self._value

    async def __aexit__(self, *_: object) -> bool:
        return False


def _auth() -> AuthContext:
    return AuthContext(user_id="usr_1", workspace_id="wrk_1", role="admin", scopes=[], kind="jwt")


def _repo() -> MagicMock:
    return MagicMock(
        get_connection_provider=AsyncMock(return_value="slack"),
        update_lookback=AsyncMock(),
        upsert_channel=AsyncMock(),
        list_channels=AsyncMock(return_value=[]),
    )


async def _select(req: ChannelSelectRequest) -> MagicMock:
    repo = _repo()
    svc = SourcesService(repository=repo)
    session = MagicMock(commit=AsyncMock())
    with patch.object(service_module, "get_session", return_value=_AsyncCtx(session)), patch.object(
        service_module, "run_in_tenant", return_value=_AsyncCtx(None)
    ):
        await svc.select_channels(_auth(), "src_1", req)
    return repo


@pytest.mark.asyncio
async def test_lookback_days_persisted_when_provided() -> None:
    req = ChannelSelectRequest(
        channels=[ChannelSelection(external_id="C1", name="#policy")], lookback_days=180
    )
    repo = await _select(req)
    repo.update_lookback.assert_awaited_once()
    assert repo.update_lookback.await_args.args[1:] == ("src_1", 180)
    repo.upsert_channel.assert_awaited_once()


@pytest.mark.asyncio
async def test_lookback_untouched_when_omitted() -> None:
    req = ChannelSelectRequest(channels=[ChannelSelection(external_id="C1", name="#policy")])
    repo = await _select(req)
    repo.update_lookback.assert_not_awaited()


def test_lookback_bounds_rejected() -> None:
    with pytest.raises(ValidationError):
        ChannelSelectRequest(channels=[], lookback_days=0)
    with pytest.raises(ValidationError):
        ChannelSelectRequest(channels=[], lookback_days=1000)


@pytest.mark.asyncio
async def test_list_channels_refreshes_expired_token_before_provider_call() -> None:
    """GitHub/Jira access tokens die in ~1h; opening the picker later must refresh
    (like the jobs layer) instead of calling the provider with a dead token → 500."""
    from datetime import UTC, datetime, timedelta

    from app.integrations.base import ChannelRef, OAuthTokens
    from app.shared.helpers.crypto import encrypt

    expired = datetime.now(UTC) - timedelta(minutes=10)
    repo = MagicMock(
        get_connection_secrets=AsyncMock(
            return_value={
                "id": "src_1",
                "provider": "jira",
                "access_token_enc": encrypt("dead-token").encode(),
                "refresh_token_enc": encrypt("refresh-1").encode(),
                "token_expires_at": expired,
            }
        ),
        list_channels=AsyncMock(return_value=[]),
        update_tokens=AsyncMock(),
    )
    integration = MagicMock(
        refresh=AsyncMock(
            return_value=OAuthTokens(
                access_token="fresh-token",
                refresh_token="refresh-2",  # rotated — must be persisted
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        ),
        list_channels=AsyncMock(return_value=[ChannelRef(external_id="PROJ", name="PROJ")]),
    )
    svc = SourcesService(repository=repo)
    session = MagicMock(commit=AsyncMock())
    with patch.object(
        service_module, "get_session", return_value=_AsyncCtx(session)
    ), patch.object(
        service_module, "run_in_tenant", return_value=_AsyncCtx(None)
    ), patch.object(service_module, "get_integration", return_value=integration):
        out = await svc.list_channels(_auth(), "src_1")

    integration.refresh.assert_awaited_once_with("refresh-1")
    # The provider is called with the refreshed token, and the rotation is persisted.
    integration.list_channels.assert_awaited_once_with("fresh-token")
    repo.update_tokens.assert_awaited_once()
    assert [c.external_id for c in out] == ["PROJ"]


def test_request_accepts_the_camelcase_keys_responses_emit() -> None:
    # GET /channels responds with camelCase (externalId, itemCount); a client echoing
    # those keys back in the PATCH must not 422 (extra='forbid' + no aliases did).
    req = ChannelSelectRequest.model_validate(
        {
            "channels": [{"externalId": "C1", "name": "#policy", "selected": True}],
            "lookbackDays": 90,
        }
    )
    assert req.channels[0].external_id == "C1"
    assert req.lookback_days == 90
    # snake_case still accepted (populate_by_name).
    req2 = ChannelSelectRequest.model_validate(
        {"channels": [{"external_id": "C2", "name": "#x"}], "lookback_days": 30}
    )
    assert req2.channels[0].external_id == "C2"
    assert req2.lookback_days == 30
