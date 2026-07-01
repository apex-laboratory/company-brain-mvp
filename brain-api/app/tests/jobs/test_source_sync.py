"""source_sync error-classification tests — the connector<->job boundary.

Covers review bug #3: an auth failure a connector reports as
:class:`ConnectorAuthError` (Slack's ``ok:false`` invalid_auth, not an HTTP 401) must
be classified as ``auth_broken`` so the connection is flagged for re-auth, not left
connected and retried forever.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.integrations.base import ConnectorAuthError
from app.jobs.tasks import source_sync as ss


class _AsyncCtx:
    """Minimal async context manager returning a fixed value."""

    def __init__(self, value: object) -> None:
        self._value = value

    async def __aenter__(self) -> object:
        return self._value

    async def __aexit__(self, *_: object) -> bool:
        return False


def _state() -> MagicMock:
    return MagicMock(
        access_token_enc=b"enc",
        provider="slack",
        external_account_id="T1",
        last_synced_at=None,
        token_expires_at=None,
        refresh_token_enc=None,
        lookback_days=90,
    )


async def _run_with_fetch_error(error: Exception) -> tuple[dict, MagicMock]:
    session = MagicMock(commit=AsyncMock())
    repo = MagicMock(get_sync_state=AsyncMock(return_value=_state()), mark_error=AsyncMock())
    integration = MagicMock(
        fetch_since=AsyncMock(side_effect=error),
        opaque_cursor=False,
        supports_channel_filter=False,
    )
    with patch.object(ss, "get_session", return_value=_AsyncCtx(session)), patch.object(
        ss, "run_in_tenant", return_value=_AsyncCtx(None)
    ), patch.object(ss, "_repo", repo), patch.object(
        ss, "get_integration", return_value=integration
    ), patch.object(ss, "_resolve_token", AsyncMock(return_value="xoxb")):
        result = await ss.source_sync({}, "wrk_1", "src_1")
    return result, repo


@pytest.mark.asyncio
async def test_connector_auth_error_marks_auth_broken() -> None:
    result, repo = await _run_with_fetch_error(ConnectorAuthError("invalid_auth"))
    repo.mark_error.assert_awaited_once()
    assert repo.mark_error.await_args.kwargs["auth_broken"] is True
    assert result == {"inserted": 0, "error": "auth_broken"}


@pytest.mark.asyncio
async def test_http_401_still_marks_auth_broken() -> None:
    err = httpx.HTTPStatusError(
        "unauthorized",
        request=httpx.Request("GET", "https://x"),
        response=httpx.Response(401, request=httpx.Request("GET", "https://x")),
    )
    result, repo = await _run_with_fetch_error(err)
    assert repo.mark_error.await_args.kwargs["auth_broken"] is True
    assert result == {"inserted": 0, "error": "auth_broken"}


@pytest.mark.asyncio
async def test_http_500_is_transient_not_auth_broken() -> None:
    err = httpx.HTTPStatusError(
        "server error",
        request=httpx.Request("GET", "https://x"),
        response=httpx.Response(500, request=httpx.Request("GET", "https://x")),
    )
    with pytest.raises(httpx.HTTPStatusError):
        await _run_with_fetch_error(err)


# ── lookback seeding + channel scoping ────────────────────────────────────────
async def _run_ok(
    state: MagicMock, integration: MagicMock, selected: list[str] | None = None
) -> MagicMock:
    """Run a successful sync; return the integration mock for call inspection."""
    session = MagicMock(commit=AsyncMock())
    repo = MagicMock(
        get_sync_state=AsyncMock(return_value=state),
        advance_sync=AsyncMock(),
        insert_event=AsyncMock(return_value=True),
        selected_channel_ids=AsyncMock(return_value=selected or []),
    )
    with patch.object(ss, "get_session", return_value=_AsyncCtx(session)), patch.object(
        ss, "run_in_tenant", return_value=_AsyncCtx(None)
    ), patch.object(ss, "_repo", repo), patch.object(
        ss, "get_integration", return_value=integration
    ), patch.object(ss, "_resolve_token", AsyncMock(return_value="tok")):
        await ss.source_sync({}, "wrk_1", "src_1")
    return integration


@pytest.mark.asyncio
async def test_first_sync_seeds_cursor_from_lookback_days() -> None:
    from datetime import UTC, datetime, timedelta

    state = _state()  # last_synced_at=None, lookback_days=90
    integration = MagicMock(
        fetch_since=AsyncMock(return_value=([], None)),
        opaque_cursor=False,
        supports_channel_filter=False,
    )
    integration = await _run_ok(state, integration)
    cursor = integration.fetch_since.await_args.args[2]
    seeded = datetime.fromisoformat(cursor)
    expected = datetime.now(UTC) - timedelta(days=90)
    assert abs((seeded - expected).total_seconds()) < 60


@pytest.mark.asyncio
async def test_selected_channels_passed_to_filtering_integration() -> None:
    integration = MagicMock(
        fetch_since=AsyncMock(return_value=([], None)),
        opaque_cursor=False,
        supports_channel_filter=True,
    )
    integration = await _run_ok(_state(), integration, selected=["C1", "C2"])
    assert integration.fetch_since.await_args.kwargs["allowed_channels"] == {"C1", "C2"}


@pytest.mark.asyncio
async def test_no_selection_means_no_channel_filter() -> None:
    integration = MagicMock(
        fetch_since=AsyncMock(return_value=([], None)),
        opaque_cursor=False,
        supports_channel_filter=True,
    )
    integration = await _run_ok(_state(), integration, selected=[])
    assert "allowed_channels" not in integration.fetch_since.await_args.kwargs
