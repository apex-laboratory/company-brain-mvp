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
