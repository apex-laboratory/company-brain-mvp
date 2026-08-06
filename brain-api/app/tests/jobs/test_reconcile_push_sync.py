"""reconcile_push_sources cron tests.

GitHub/Slack have no periodic poll — they rely entirely on webhooks, and a
connection's ``last_synced_at`` only advances on a *successful* sync, so a
dropped webhook (outage, lapsed provider redelivery window) is indistinguishable
from a quiet source. This cron re-syncs push-delivery connections hourly as a
backstop. Asserts: only push providers are targeted, one dedup-keyed
``source_sync`` is enqueued per connection, and no providers → no DB query.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.jobs.tasks import reconcile_push_sync as rps


class _AsyncCtx:
    def __init__(self, value: object) -> None:
        self._value = value

    async def __aenter__(self) -> object:
        return self._value

    async def __aexit__(self, *_: object) -> bool:
        return False


def test_push_providers_derived_from_registry() -> None:
    registry = {
        "notion": MagicMock(push_delivery=False),
        "slack": MagicMock(push_delivery=True),
        "github": MagicMock(spec=[]),  # no attribute at all → push assumed
    }
    with patch.object(rps, "REGISTRY", registry):
        assert set(rps._push_providers()) == {"slack", "github"}


def test_github_is_push_in_the_real_registry() -> None:
    # Regression guard: if GitHub ever loses webhooks, remove the flag knowingly.
    assert "github" in rps._push_providers()
    assert "notion" not in rps._push_providers()


@pytest.mark.asyncio
async def test_enqueues_one_sync_per_connection_with_bucketed_job_id() -> None:
    repo = MagicMock(
        list_pollable_connections=AsyncMock(
            return_value=[("src_1", "wrk_1"), ("src_2", "wrk_2")]
        )
    )
    enqueue = AsyncMock()
    with patch.object(rps, "get_session", return_value=_AsyncCtx(MagicMock())), patch.object(
        rps, "_repo", repo
    ), patch.object(rps, "enqueue", enqueue), patch.object(
        rps, "_push_providers", return_value=["github"]
    ):
        result = await rps.reconcile_push_sources({})

    assert result == {"enqueued": 2}
    repo.list_pollable_connections.assert_awaited_once()
    assert repo.list_pollable_connections.await_args.args[1] == ["github"]
    calls = enqueue.await_args_list
    assert [c.args for c in calls] == [
        ("source_sync", "wrk_1", "src_1"),
        ("source_sync", "wrk_2", "src_2"),
    ]
    # Dedup key present and distinct per source within the tick's bucket.
    ids = [c.kwargs["_job_id"] for c in calls]
    assert ids[0].startswith("reconcile-push:src_1:") and ids[1].startswith("reconcile-push:src_2:")
    assert ids[0].rsplit(":", 1)[1] == ids[1].rsplit(":", 1)[1]


@pytest.mark.asyncio
async def test_no_push_providers_skips_db_entirely() -> None:
    repo = MagicMock(list_pollable_connections=AsyncMock())
    with patch.object(rps, "_repo", repo), patch.object(
        rps, "_push_providers", return_value=[]
    ):
        result = await rps.reconcile_push_sources({})
    assert result == {"enqueued": 0}
    repo.list_pollable_connections.assert_not_awaited()
