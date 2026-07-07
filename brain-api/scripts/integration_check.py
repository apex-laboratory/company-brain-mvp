"""KAN-2 integration check against the provisioned local DB.

Exercises the real repository SQL (ON CONFLICT constraint names, JSONB casts) and
RLS enforcement end-to-end — the parts unit tests can't cover because they fake
the session. Run after ``provision_test_db.py``.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config.database import get_session
from app.config.settings import settings
from app.integrations.base import RawEvent
from app.jobs.repository import JobsRepository
from app.modules.sources.repository import SourcesRepository
from app.shared.helpers.crypto import encrypt, sha256_hash
from app.shared.middleware.with_tenant import run_in_tenant

# Second engine connected as the unprivileged brain_app role so RLS is enforced
# (the default cb connection is superuser and bypasses RLS).
_app_url = settings.database_url.split("://", 1)[1].split("@", 1)[1]
app_engine = create_async_engine(f"postgresql+asyncpg://brain_app:brain_app@{_app_url}")
AppSession = async_sessionmaker(app_engine, expire_on_commit=False)


@asynccontextmanager
async def get_app_session():
    async with AppSession() as session:
        yield session

WS = "wrk_test"
USER = "usr_test"

sources = SourcesRepository()
jobs = JobsRepository()
passed: list[str] = []
failed: list[str] = []


def check(name: str, cond: bool) -> None:
    (passed if cond else failed).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")


async def seed() -> None:
    async with get_session() as s:
        await s.execute(text("INSERT INTO users (id, email) VALUES (:i, :e) "
                             "ON CONFLICT (id) DO NOTHING").bindparams(i=USER, e="t@example.com"))
        await s.execute(text("INSERT INTO workspaces (id, name, slug) VALUES (:i, 'T', 't') "
                             "ON CONFLICT (id) DO NOTHING").bindparams(i=WS))
        await s.execute(text(
            "INSERT INTO workspace_members (id, workspace_id, user_id, role) "
            "VALUES ('mem_1', :w, :u, 'admin') ON CONFLICT (id) DO NOTHING"
        ).bindparams(w=WS, u=USER))
        await s.commit()


async def test_oauth_state() -> None:
    print("oauth_states (service-role, single-use):")
    state_hash = sha256_hash("state-token-1")
    async with get_session() as s:
        await sources.create_oauth_state(
            s, state_hash=state_hash, provider="notion",
            redirect_uri="http://cb", user_id=USER, workspace_id=WS,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
    async with get_session() as s:
        first = await sources.consume_oauth_state(s, state_hash=state_hash, provider="notion", now=datetime.now(UTC))
    async with get_session() as s:
        second = await sources.consume_oauth_state(s, state_hash=state_hash, provider="notion", now=datetime.now(UTC))
    check("state consumed once returns the row", first is not None and first.workspace_id == WS)
    check("replayed state returns None (single-use)", second is None)


async def test_connection_upsert_and_rls() -> None:
    print("source_connections (ON CONFLICT + RLS admin-only):")
    async with get_session() as s:
        async with run_in_tenant(s, WS, USER, "admin"):
            cid = await sources.upsert_connection(
                s, connection_id="src_1", workspace_id=WS, provider="notion",
                name="Acme", access_token_enc=encrypt("tok-a").encode(),
                refresh_token_enc=None, token_expires_at=None, scopes=[],
                external_account_id="acct-1", connected_by=USER,
            )
            # Re-upsert same (workspace, provider, account) → updates, no dup row.
            cid2 = await sources.upsert_connection(
                s, connection_id="src_2", workspace_id=WS, provider="notion",
                name="Acme HQ", access_token_enc=encrypt("tok-b").encode(),
                refresh_token_enc=None, token_expires_at=None, scopes=[],
                external_account_id="acct-1", connected_by=USER,
            )
            rows = await sources.list_connections(s)
            await s.commit()
    check("ON CONFLICT constraint resolves (upsert returns id)", cid == "src_1")
    check("re-upsert hits same row (no duplicate)", cid2 == "src_1" and len(rows) == 1)
    check("updated name persisted via ON CONFLICT", rows[0]["name"] == "Acme HQ")

    # RLS (enforced via the unprivileged brain_app role): admin sees the row,
    # viewer does not (source_connections is admin-only).
    async with get_app_session() as s:
        async with run_in_tenant(s, WS, USER, "admin"):
            admin_rows = await sources.list_connections(s)
    async with get_app_session() as s:
        async with run_in_tenant(s, WS, USER, "viewer"):
            viewer_rows = await sources.list_connections(s)
    check("RLS admin role sees admin-only connection", len(admin_rows) == 1)
    check("RLS hides admin-only connection from viewer", viewer_rows == [])


async def test_event_idempotency() -> None:
    print("source_events (idempotent insert):")
    event = RawEvent(
        provider="notion", source_id="p1", external_event_id="p1:e1",
        event_type="page", actor={}, content="hi",
        created_at=datetime.now(UTC), url="", raw={"id": "p1", "k": "v"},
    )
    async with get_session() as s:
        async with run_in_tenant(s, WS, USER, "admin"):
            first = await jobs.insert_event(s, WS, event)
            second = await jobs.insert_event(s, WS, event)  # duplicate
            await jobs.advance_sync(s, "src_1", datetime.now(UTC))
            await s.commit()
    check("first insert succeeds", first is True)
    check("duplicate insert is a no-op (idempotent)", second is False)


async def main() -> None:
    await seed()
    await test_oauth_state()
    await test_connection_upsert_and_rls()
    await test_event_idempotency()
    print(f"\n{len(passed)} passed, {len(failed)} failed")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
