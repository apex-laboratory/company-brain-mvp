"""poll_pull_sources cron tests (KAN-2).

Notion has no webhooks, so without this cron a Notion connection syncs once
during the onboarding sweep and then never again. Asserts: only pull-only
providers (``push_delivery = False``) are polled, one dedup-keyed ``source_sync``
is enqueued per connection, and no providers → no DB query.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.jobs.tasks import poll_sync as ps


class _AsyncCtx:
    def __init__(self, value: object) -> None:
        self._value = value

    async def __aenter__(self) -> object:
        return self._value

    async def __aexit__(self, *_: object) -> bool:
        return False


def test_pull_providers_derived_from_registry() -> None:
    registry = {
        "notion": MagicMock(push_delivery=False),
        "slack": MagicMock(push_delivery=True),
        "github": MagicMock(spec=[]),  # no attribute at all → push assumed
    }
    with patch.object(ps, "REGISTRY", registry):
        assert ps._pull_providers() == ["notion"]


def test_notion_is_pull_only_in_the_real_registry() -> None:
    # Regression guard: if Notion ever grows webhooks, remove the flag knowingly.
    assert "notion" in ps._pull_providers()
    assert "slack" not in ps._pull_providers()


def test_polling_only_providers_are_registered_pull_only() -> None:
    # Zendesk/Jira ship polling-first (webhook registration is a follow-up). If either
    # drops out of the pull set it syncs once at onboarding and then never again.
    pull = ps._pull_providers()
    assert "zendesk" in pull
    assert "jira" in pull


@pytest.mark.asyncio
async def test_enqueues_one_sync_per_connection_with_bucketed_job_id() -> None:
    repo = MagicMock(
        list_pollable_connections=AsyncMock(
            return_value=[("src_1", "wrk_1"), ("src_2", "wrk_2")]
        )
    )
    enqueue = AsyncMock()
    with patch.object(ps, "get_session", return_value=_AsyncCtx(MagicMock())), patch.object(
        ps, "_repo", repo
    ), patch.object(ps, "enqueue", enqueue), patch.object(
        ps, "_pull_providers", return_value=["notion"]
    ):
        result = await ps.poll_pull_sources({})

    assert result == {"enqueued": 2}
    repo.list_pollable_connections.assert_awaited_once()
    assert repo.list_pollable_connections.await_args.args[1] == ["notion"]
    calls = enqueue.await_args_list
    assert [c.args for c in calls] == [
        ("source_sync", "wrk_1", "src_1"),
        ("source_sync", "wrk_2", "src_2"),
    ]
    # Dedup key present and distinct per source within the tick's bucket.
    ids = [c.kwargs["_job_id"] for c in calls]
    assert ids[0].startswith("poll-sync:src_1:") and ids[1].startswith("poll-sync:src_2:")
    assert ids[0].rsplit(":", 1)[1] == ids[1].rsplit(":", 1)[1]


@pytest.mark.asyncio
async def test_no_pull_providers_skips_db_entirely() -> None:
    repo = MagicMock(list_pollable_connections=AsyncMock())
    with patch.object(ps, "_repo", repo), patch.object(
        ps, "_pull_providers", return_value=[]
    ):
        result = await ps.poll_pull_sources({})
    assert result == {"enqueued": 0}
    repo.list_pollable_connections.assert_not_awaited()
