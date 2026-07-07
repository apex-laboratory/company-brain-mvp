"""webhook_ingest routing tests (KAN-2).

The same provider account (Slack team, GitHub installation) can be connected in
more than one workspace; a single delivery must fan out to *all* of them — matching
only the first would leave the others permanently stale (push providers are never
polled by the cron).
"""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.integrations.base import RawEvent
from app.jobs.tasks import webhook_ingest as wi


class _AsyncCtx:
    def __init__(self, value: object) -> None:
        self._value = value

    async def __aenter__(self) -> object:
        return self._value

    async def __aexit__(self, *_: object) -> bool:
        return False


def _event() -> RawEvent:
    return RawEvent(
        provider="slack",
        source_id="m1",
        external_event_id="m1:e",
        event_type="message",
        actor={"id": "", "email": "", "name": ""},
        content="hi",
        created_at=datetime.now(UTC),
        url="",
        raw={},
    )


def _wire(repo, integration):  # noqa: ANN001
    return (
        patch.object(wi, "get_session", return_value=_AsyncCtx(MagicMock(commit=AsyncMock()))),
        patch.object(wi, "run_in_tenant", return_value=_AsyncCtx(None)),
        patch.object(wi, "_repo", repo),
        patch.object(wi, "get_integration", return_value=integration),
    )


@pytest.mark.asyncio
async def test_event_fans_out_to_every_workspace_with_the_account() -> None:
    repo = MagicMock(
        resolve_all_by_account=AsyncMock(
            return_value=[("src_1", "wrk_1"), ("src_2", "wrk_2")]
        ),
        insert_event=AsyncMock(return_value=True),
    )
    integration = MagicMock(normalize=MagicMock(return_value=_event()))
    p = _wire(repo, integration)
    with p[0], p[1], p[2], p[3]:
        result = await wi.webhook_ingest({}, "slack", {"team_id": "T1", "id": "m1"})

    assert result == {"inserted": 2}
    workspaces = [c.args[1] for c in repo.insert_event.await_args_list]
    assert workspaces == ["wrk_1", "wrk_2"]


@pytest.mark.asyncio
async def test_unknown_account_is_skipped() -> None:
    repo = MagicMock(resolve_all_by_account=AsyncMock(return_value=[]))
    integration = MagicMock()
    p = _wire(repo, integration)
    with p[0], p[1], p[2], p[3]:
        result = await wi.webhook_ingest({}, "slack", {"team_id": "T-gone", "id": "m1"})
    assert result == {"inserted": 0, "skipped": "no_connection"}
