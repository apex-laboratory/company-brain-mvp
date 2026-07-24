"""source_sync job orchestration tests (KAN-2).

Exercises the job's control flow — token decryption, fetch, idempotent insert
counting, cursor advance — with the session, integration, and repository faked,
so it runs without Postgres or Redis.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
import pytest

from app.integrations.base import ChannelRef, RawEvent, RawItem
from app.jobs.repository import SyncState
from app.jobs.tasks import source_sync as job
from app.shared.helpers.crypto import encrypt


class _FakeSession:
    async def execute(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None

    async def commit(self) -> None:
        return None


@asynccontextmanager
async def _fake_get_session():
    yield _FakeSession()


class _FakeIntegration:
    provider = "notion"

    def __init__(self, items: list[RawItem], next_cursor: str | None) -> None:
        self._items = items
        self._next_cursor = next_cursor

    async def fetch_since(self, token, channel, cursor):  # noqa: ANN001
        assert token == "plain-token"
        assert isinstance(channel, ChannelRef)
        return self._items, self._next_cursor

    def normalize(self, item: RawItem) -> RawEvent:
        return RawEvent(
            provider="notion",
            source_id=item.external_id,
            external_event_id=f"{item.external_id}:e",
            event_type="page",
            actor={"id": "", "email": "", "name": ""},
            content="",
            created_at=datetime.now(UTC),
            url="",
            raw=item.payload,
        )


class _FakeRepo:
    def __init__(self, state: SyncState, dup_ids: set[str] | None = None) -> None:
        self._state = state
        self._dup_ids = dup_ids or set()
        self.advanced_to: datetime | None = None
        self.advanced_cursor: str | None = None
        self.error_marked = False
        self.last_sweep_id: str | None = None

    async def get_sync_state(self, session, source_id):  # noqa: ANN001
        return self._state

    async def insert_event(  # noqa: ANN001
        self, session, workspace_id, event, sweep_id=None, *, source_connection_id=None
    ) -> str | None:
        self.last_sweep_id = sweep_id
        self.last_connection_id = source_connection_id
        # Real repo returns the new event id (or None on duplicate).
        return None if event.source_id in self._dup_ids else f"evt_{event.source_id}"

    async def advance_sync(self, session, source_id, synced_at, sync_cursor=None) -> None:  # noqa: ANN001
        self.advanced_to = synced_at
        self.advanced_cursor = sync_cursor

    async def mark_error(self, session, source_id, *, auth_broken) -> None:  # noqa: ANN001
        self.error_marked = True


def _state() -> SyncState:
    return SyncState(
        id="src_1",
        provider="notion",
        access_token_enc=encrypt("plain-token").encode(),
        refresh_token_enc=None,
        token_expires_at=None,  # Notion tokens don't expire → no refresh path
        external_account_id="ws-1",
        last_synced_at=None,
    )


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    integration: _FakeIntegration,
    repo: _FakeRepo,
) -> list[tuple]:
    """Wire the fakes; return a list that captures ``enqueue`` calls."""
    enqueued: list[tuple] = []

    async def _fake_enqueue(function, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        enqueued.append((function, args, kwargs))

    monkeypatch.setattr(job, "get_tenant_session", _fake_get_session)
    monkeypatch.setattr(job, "get_integration", lambda provider: integration)
    monkeypatch.setattr(job, "_repo", repo)
    monkeypatch.setattr(job, "enqueue", _fake_enqueue)
    return enqueued


async def test_sync_inserts_new_events_and_advances_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items = [RawItem(external_id=f"p{i}", payload={}) for i in range(3)]
    integration = _FakeIntegration(items, "2026-06-13T10:00:00+00:00")
    repo = _FakeRepo(_state())
    enqueued = _wire(monkeypatch, integration, repo)

    result = await job.source_sync({}, "wrk_1", "src_1")

    assert result == {"inserted": 3}
    assert repo.advanced_to == datetime(2026, 6, 13, 10, 0, tzinfo=UTC)
    # Non-sweep sync extracts each new event immediately.
    assert [c[0] for c in enqueued] == ["extract_event"] * 3
    assert enqueued[0][1] == ("wrk_1", "evt_p0")


async def test_sweep_sync_defers_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    # When invoked with a sweep_id, events are stamped and extraction is left to
    # the batched sweep_extract pass — no per-event enqueue here.
    items = [RawItem(external_id="p1", payload={})]
    integration = _FakeIntegration(items, "2026-06-13T10:00:00+00:00")
    repo = _FakeRepo(_state())
    enqueued = _wire(monkeypatch, integration, repo)

    result = await job.source_sync({}, "wrk_1", "src_1", sweep_id="swp_1")

    assert result == {"inserted": 1}
    assert repo.last_sweep_id == "swp_1"
    assert enqueued == []


async def test_sync_is_idempotent_on_duplicates(monkeypatch: pytest.MonkeyPatch) -> None:
    items = [RawItem(external_id="p1", payload={}), RawItem(external_id="p2", payload={})]
    integration = _FakeIntegration(items, "2026-06-13T10:00:00+00:00")
    repo = _FakeRepo(_state(), dup_ids={"p1", "p2"})  # both already ingested
    _wire(monkeypatch, integration, repo)

    result = await job.source_sync({}, "wrk_1", "src_1")

    assert result == {"inserted": 0}


async def test_sync_marks_auth_broken_on_401(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Failing(_FakeIntegration):
        async def fetch_since(self, token, channel, cursor):  # noqa: ANN001
            raise httpx.HTTPStatusError(
                "unauthorized",
                request=httpx.Request("POST", "https://api.notion.com/v1/search"),
                response=httpx.Response(401),
            )

    repo = _FakeRepo(_state())
    _wire(monkeypatch, _Failing([], None), repo)

    result = await job.source_sync({}, "wrk_1", "src_1")

    assert result == {"inserted": 0, "error": "auth_broken"}
    assert repo.error_marked is True


async def test_sync_rate_limited_403_is_transient_not_auth_broken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # GitHub returns 403 for rate limiting; that must NOT flip the connection to
    # status='error' (which nothing recovers from) — it re-raises so ARQ retries.
    class _RateLimited(_FakeIntegration):
        async def fetch_since(self, token, channel, cursor):  # noqa: ANN001
            raise httpx.HTTPStatusError(
                "rate limited",
                request=httpx.Request("GET", "https://api.github.com/repos/a/b/issues"),
                response=httpx.Response(403, headers={"x-ratelimit-remaining": "0"}),
            )

    class _Repo(_FakeRepo):
        auth_broken: bool | None = None

        async def mark_error(self, session, source_id, *, auth_broken) -> None:  # noqa: ANN001
            self.error_marked = True
            self.auth_broken = auth_broken

    repo = _Repo(_state())
    _wire(monkeypatch, _RateLimited([], None), repo)

    with pytest.raises(httpx.HTTPStatusError):  # re-raised → ARQ retries
        await job.source_sync({}, "wrk_1", "src_1")
    assert repo.auth_broken is False  # sync_status flagged, connection NOT bricked


async def test_sync_permission_403_still_marks_auth_broken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A 403 with no rate-limit signal is a genuine permission/auth failure.
    class _Forbidden(_FakeIntegration):
        async def fetch_since(self, token, channel, cursor):  # noqa: ANN001
            raise httpx.HTTPStatusError(
                "forbidden",
                request=httpx.Request("GET", "https://example.com"),
                response=httpx.Response(403),
            )

    repo = _FakeRepo(_state())
    _wire(monkeypatch, _Forbidden([], None), repo)

    result = await job.source_sync({}, "wrk_1", "src_1")
    assert result == {"inserted": 0, "error": "auth_broken"}
    assert repo.error_marked is True


class _OpaqueIntegration(_FakeIntegration):
    """A token-cursor provider (like Google) whose cursor is an opaque string."""

    opaque_cursor = True

    async def fetch_since(self, token, channel, cursor):  # noqa: ANN001
        # The incoming cursor is the stored opaque sync_cursor, not an ISO timestamp.
        assert cursor == "pageTokenABC"
        return self._items, "pageTokenXYZ"


async def test_sync_roundtrips_opaque_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    # Regression guard: an opaque cursor (Drive pageToken / Gmail historyId) must be
    # stored verbatim in sync_cursor — never passed through _parse_iso (which would
    # crash on a non-timestamp string).
    state = SyncState(
        id="src_1",
        provider="google_drive",
        access_token_enc=encrypt("plain-token").encode(),
        refresh_token_enc=None,
        token_expires_at=None,
        external_account_id="ada@acme.com",
        last_synced_at=None,
        sync_cursor="pageTokenABC",
    )
    items = [RawItem(external_id="f1", payload={})]
    repo = _FakeRepo(state)
    _wire(monkeypatch, _OpaqueIntegration(items, None), repo)

    result = await job.source_sync({}, "wrk_1", "src_1")

    assert result == {"inserted": 1}
    assert repo.advanced_cursor == "pageTokenXYZ"  # stored opaque, not parsed
    assert repo.advanced_to is not None  # last_synced_at still bumped for freshness


async def test_sync_chains_next_chunk_on_backfill_continuation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An opaque-cursor provider that returns a BACKFILL_CURSOR_PREFIX continuation
    # cursor must trigger a chained source_sync so a large bootstrap completes without
    # waiting for the next push/poll.
    from app.integrations.base import BACKFILL_CURSOR_PREFIX

    continuation = f"{BACKFILL_CURSOR_PREFIX}123|hist-9|pageTok"

    class _Chunked(_FakeIntegration):
        opaque_cursor = True

        async def fetch_since(self, token, channel, cursor, **kwargs):  # noqa: ANN001, ANN003
            return self._items, continuation

    state = SyncState(
        id="src_1",
        provider="gmail",
        access_token_enc=encrypt("plain-token").encode(),
        refresh_token_enc=None,
        token_expires_at=None,
        external_account_id="ada@acme.com",
        last_synced_at=None,
        sync_cursor=None,
    )
    repo = _FakeRepo(state)
    enqueued = _wire(monkeypatch, _Chunked([RawItem(external_id="m1", payload={})], None), repo)

    result = await job.source_sync({}, "wrk_1", "src_1")

    assert result == {"inserted": 1, "backfill": "continues"}
    assert repo.advanced_cursor == continuation  # continuation persisted first
    # Non-sweep run: the inserted event still extracts, then the next chunk chains.
    assert [(fn, args) for fn, args, _ in enqueued] == [
        ("extract_event", ("wrk_1", "evt_m1")),
        ("source_sync", ("wrk_1", "src_1", None)),
    ]
    assert enqueued[1][2] == {"_job_id": "backfill-chain:src_1"}


async def test_sync_skips_when_no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    state = SyncState(
        id="src_1",
        provider="notion",
        access_token_enc=None,
        refresh_token_enc=None,
        token_expires_at=None,
        external_account_id=None,
        last_synced_at=None,
    )
    repo = _FakeRepo(state)
    _wire(monkeypatch, _FakeIntegration([], None), repo)

    result = await job.source_sync({}, "wrk_1", "src_1")

    assert result == {"inserted": 0, "skipped": "no_connection"}
