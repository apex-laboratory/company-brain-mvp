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
        self.error_marked = False

    async def get_sync_state(self, session, source_id):  # noqa: ANN001
        return self._state

    async def insert_event(self, session, workspace_id, event) -> bool:  # noqa: ANN001
        return event.source_id not in self._dup_ids

    async def advance_sync(self, session, source_id, synced_at) -> None:  # noqa: ANN001
        self.advanced_to = synced_at

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


def _wire(monkeypatch: pytest.MonkeyPatch, integration: _FakeIntegration, repo: _FakeRepo) -> None:
    monkeypatch.setattr(job, "get_session", _fake_get_session)
    monkeypatch.setattr(job, "get_integration", lambda provider: integration)
    monkeypatch.setattr(job, "_repo", repo)


async def test_sync_inserts_new_events_and_advances_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items = [RawItem(external_id=f"p{i}", payload={}) for i in range(3)]
    integration = _FakeIntegration(items, "2026-06-13T10:00:00+00:00")
    repo = _FakeRepo(_state())
    _wire(monkeypatch, integration, repo)

    result = await job.source_sync({}, "wrk_1", "src_1")

    assert result == {"inserted": 3}
    assert repo.advanced_to == datetime(2026, 6, 13, 10, 0, tzinfo=UTC)


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
