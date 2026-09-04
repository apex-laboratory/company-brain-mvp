"""Agent session lifecycle tests (agent-builder-plan §5.3, §5.4, phase 4).

Two things are worth testing here that would be expensive to learn in production.

**The ordering.** Session create makes four vendor calls around one insert, and
the insert is deliberately in the middle. Sending the first message before it
would leave a *working, billing* agent that no row references if the write then
failed — unreachable from every surface and invisible to the concurrency cap. The
order assertions below are the only thing holding that in place.

**The wall.** ``agent_sessions`` has no transcript column, and nothing about the
tests would fail if a well-meaning change started passing message text into a
repository call — the mock would take it happily. So there is an explicit sweep
asserting the user's message reaches Anthropic and reaches no repository call at
all, in the same shape as the access-token sweep phase 2 uses for credentials.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules.agents.schemas import (
    MAX_LIVE_SESSIONS_PER_USER,
    AgentEventSendRequest,
    AgentSessionCreateRequest,
)
from app.modules.agents.sessions import AgentSessionsService
from app.shared.errors.app_error import ConflictError, NotFoundError
from app.tests.agents.test_agents import _AsyncCtx, _auth, _row

_MESSAGE = "reconcile the September refunds"


def _session_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": "ass_1",
        "workspace_id": "wrk_1",
        "agent_id": "agt_1",
        "user_id": "usr_1",
        "anthropic_session_id": "ses_remote_1",
        "anthropic_agent_version": 2,
        "title": None,
        "status": "running",
        "stop_reason": None,
        "list_cost_cents": None,
        "input_tokens": None,
        "output_tokens": None,
        "started_at": datetime.now(UTC),
        "ended_at": None,
    }
    row.update(overrides)
    return row


def _repo(**overrides: Any) -> MagicMock:
    repo = MagicMock()
    repo.get = AsyncMock(return_value=_row())
    repo.get_session_row = AsyncMock(return_value=_session_row())
    repo.list_sessions = AsyncMock(return_value=[_session_row()])
    repo.insert_session = AsyncMock(return_value=_session_row())
    repo.count_live_sessions = AsyncMock(return_value=0)
    repo.update_session_state = AsyncMock(return_value=_session_row())
    repo.resolve_session_tenant = AsyncMock(
        return_value={"id": "ass_1", "workspace_id": "wrk_1", "user_id": "usr_1"}
    )
    for key, value in overrides.items():
        setattr(repo, key, value)
    return repo


def _anthropic(order: list[str] | None = None) -> MagicMock:
    """The vendor seam, optionally recording call order.

    ``side_effect`` rather than replacing the attribute: wrapping an ``AsyncMock``
    in a plain function destroys ``await_args`` and every assertion built on it.
    """
    client = MagicMock()
    values: dict[str, Any] = {
        "ensure_environment": "env_1",
        "create_session": {"id": "ses_remote_1", "status": "running", "agentVersion": 2},
        "send_events": {"events": []},
        "get_session": {
            "id": "ses_remote_1",
            "status": "idle",
            "inputTokens": 120,
            "outputTokens": 45,
        },
        "archive_session": None,
        "list_events": {"data": [], "next_page": None},
    }
    for name, value in values.items():
        mock = AsyncMock(return_value=value)
        if order is not None:
            mock.side_effect = _record(order, name, value)
        setattr(client, name, mock)
    return client


def _record(order: list[str], name: str, value: Any) -> Any:
    async def _call(*_: Any, **__: Any) -> Any:
        order.append(name)
        return value

    return _call


def _credentials(order: list[str] | None = None) -> MagicMock:
    service = MagicMock()
    service.ensure_user_vault = AsyncMock(return_value="vlt_user")
    service.ensure_brain_credential = AsyncMock(return_value="vlt_workspace")
    if order is not None:
        service.ensure_user_vault.side_effect = _record(order, "user_vault", "vlt_user")
        service.ensure_brain_credential.side_effect = _record(
            order, "brain_vault", "vlt_workspace"
        )
    return service


def _service(
    repo: MagicMock, anthropic: MagicMock, credentials: MagicMock | None = None
) -> AgentSessionsService:
    return AgentSessionsService(
        repository=repo, anthropic=anthropic, credentials=credentials or _credentials()
    )


def _patched(tenant: MagicMock | None = None, order: list[str] | None = None) -> Any:
    """The context managers every session-service method opens."""
    tenant = tenant or _tenant(order)

    def _run_in_tenant(*_: Any, **__: Any) -> Any:
        return _AsyncCtx(tenant)

    return patch.multiple(
        "app.modules.agents.sessions",
        get_tenant_session=MagicMock(return_value=_AsyncCtx(MagicMock())),
        get_session=MagicMock(return_value=_AsyncCtx(MagicMock())),
        run_in_tenant=MagicMock(side_effect=_run_in_tenant),
    )


def _tenant(order: list[str] | None = None) -> MagicMock:
    tenant = MagicMock()

    async def _commit() -> None:
        if order is not None:
            order.append("commit")

    tenant.commit = AsyncMock(side_effect=_commit)
    return tenant


# ── create ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_attaches_both_vaults_to_a_grounded_session() -> None:
    """§5.4: the user's connectors *and* the workspace's query_brain credential.

    Attached here or never — ``sessions.update`` rejects ``vault_ids`` outright,
    so a session created without them runs unauthorized for its whole life.
    """
    repo, anthropic = _repo(), _anthropic()
    with _patched():
        await _service(repo, anthropic).create_session(
            _auth(), "agt_1", AgentSessionCreateRequest(message=_MESSAGE)
        )

    assert anthropic.create_session.await_args.kwargs["vault_ids"] == [
        "vlt_user",
        "vlt_workspace",
    ]


@pytest.mark.asyncio
async def test_an_ungrounded_session_gets_only_the_users_vault() -> None:
    repo = _repo(get=AsyncMock(return_value=_row(ground_in_brain=False)))
    anthropic = _anthropic()
    credentials = _credentials()
    with _patched():
        await _service(repo, anthropic, credentials).create_session(
            _auth(), "agt_1", AgentSessionCreateRequest(message=_MESSAGE)
        )

    assert anthropic.create_session.await_args.kwargs["vault_ids"] == ["vlt_user"]
    credentials.ensure_brain_credential.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_row_is_written_before_the_first_message_is_sent() -> None:
    """The load-bearing ordering. See the module docstring.

    A working agent that no row references is unreachable, uncapped and still
    billing; a session row whose first message failed is one the user can see and
    retry into.
    """
    order: list[str] = []
    repo = _repo()

    async def _insert(*_: Any, **__: Any) -> dict[str, Any]:
        order.append("insert")
        return _session_row()

    repo.insert_session = AsyncMock(side_effect=_insert)

    with _patched(order=order):
        await _service(repo, _anthropic(order)).create_session(
            _auth(), "agt_1", AgentSessionCreateRequest(message=_MESSAGE)
        )

    assert order.index("create_session") < order.index("insert")
    assert order.index("insert") < order.index("send_events")
    # And no vendor call happened between opening a transaction and committing it.
    assert order.index("commit") < order.index("send_events")


@pytest.mark.asyncio
async def test_the_session_pins_the_agents_current_version() -> None:
    """An agent edited mid-session must not retroactively change a past run."""
    repo, anthropic = _repo(), _anthropic()
    with _patched():
        await _service(repo, anthropic).create_session(
            _auth(), "agt_1", AgentSessionCreateRequest(message=_MESSAGE)
        )

    assert anthropic.create_session.await_args.kwargs["agent_version"] == 2
    assert repo.insert_session.await_args.kwargs["anthropic_agent_version"] == 2


@pytest.mark.asyncio
async def test_an_unsynced_agent_cannot_start_a_session() -> None:
    """A draft whose sync failed points at ``save again``, not at a silent create.

    Creating the Anthropic agent here would mint one on a *read-shaped* request,
    which is how orphaned agent objects accumulate.
    """
    repo = _repo(get=AsyncMock(return_value=_row(anthropic_agent_id=None)))
    anthropic = _anthropic()
    with _patched(), pytest.raises(ConflictError, match="not finished syncing"):
        await _service(repo, anthropic).create_session(
            _auth(), "agt_1", AgentSessionCreateRequest(message=_MESSAGE)
        )

    anthropic.create_session.assert_not_awaited()
    anthropic.create_agent.assert_not_called()


@pytest.mark.asyncio
async def test_the_concurrency_cap_refuses_before_any_vendor_call() -> None:
    """Our cost control, since Managed Agents exposes no session budget.

    Refusing *before* the network matters: a cap that still paid for a vault
    round-trip and an environment lookup would be a cap on sessions and not on
    spend.
    """
    repo = _repo(
        count_live_sessions=AsyncMock(return_value=MAX_LIVE_SESSIONS_PER_USER)
    )
    anthropic = _anthropic()
    credentials = _credentials()
    with _patched(), pytest.raises(ConflictError, match="already have"):
        await _service(repo, anthropic, credentials).create_session(
            _auth(), "agt_1", AgentSessionCreateRequest(message=_MESSAGE)
        )

    anthropic.create_session.assert_not_awaited()
    credentials.ensure_user_vault.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_invisible_agent_is_a_404_not_a_session() -> None:
    repo = _repo(get=AsyncMock(return_value=None))
    anthropic = _anthropic()
    with _patched(), pytest.raises(NotFoundError):
        await _service(repo, anthropic).create_session(
            _auth(), "agt_other", AgentSessionCreateRequest(message=_MESSAGE)
        )

    anthropic.create_session.assert_not_awaited()


# ── the wall ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_users_message_reaches_anthropic_and_no_repository_call() -> None:
    """``agent_sessions`` has no column for it and must never be handed one.

    A mock repository would accept message text without complaint, so nothing
    else in this file would fail if a change started passing it. This is the
    assertion that would.
    """
    repo, anthropic = _repo(), _anthropic()
    with _patched():
        await _service(repo, anthropic).create_session(
            _auth(), "agt_1", AgentSessionCreateRequest(message=_MESSAGE)
        )

    for name in dir(repo):
        if name.startswith("_"):
            continue
        for call in getattr(getattr(repo, name), "await_args_list", []) or []:
            assert _MESSAGE not in repr(call), f"the message reached repo.{name}"

    # Not vacuous: it did reach the vendor, which is the whole point.
    assert _MESSAGE in repr(anthropic.send_events.await_args)


# ── events ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sent,expected",
    [
        (
            {"type": "user.message", "text": "hello"},
            {"type": "user.message", "content": [{"type": "text", "text": "hello"}]},
        ),
        ({"type": "user.interrupt"}, {"type": "user.interrupt"}),
        (
            {"type": "user.tool_confirmation", "toolUseId": "tu_1", "result": "allow"},
            {
                "type": "user.tool_confirmation",
                "tool_use_id": "tu_1",
                "result": "allow",
            },
        ),
        (
            {
                "type": "user.tool_confirmation",
                "toolUseId": "tu_1",
                "result": "deny",
                "denyMessage": "not that repo",
            },
            {
                "type": "user.tool_confirmation",
                "tool_use_id": "tu_1",
                "result": "deny",
                "deny_message": "not that repo",
            },
        ),
    ],
    ids=["message", "interrupt", "allow", "deny"],
)
async def test_each_event_translates_to_its_vendor_shape(
    sent: dict[str, Any], expected: dict[str, Any]
) -> None:
    """Translated per type rather than dumped, so a field cannot cross by accident."""
    repo, anthropic = _repo(), _anthropic()
    with _patched():
        await _service(repo, anthropic).send_events(
            _auth(), "ass_1", AgentEventSendRequest.model_validate({"events": [sent]})
        )

    assert anthropic.send_events.await_args.args[1] == [expected]


@pytest.mark.asyncio
async def test_events_go_to_the_sessions_vendor_id_not_the_path_id() -> None:
    repo, anthropic = _repo(), _anthropic()
    with _patched():
        await _service(repo, anthropic).send_events(
            _auth(),
            "ass_1",
            AgentEventSendRequest.model_validate(
                {"events": [{"type": "user.interrupt"}]}
            ),
        )

    assert anthropic.send_events.await_args.args[0] == "ses_remote_1"


@pytest.mark.asyncio
async def test_an_ended_session_refuses_new_events() -> None:
    """Anthropic would reject it too, as a 400 about a session id. Saying it here
    lets the client render "this run has finished" instead of "bad request"."""
    repo = _repo(
        get_session_row=AsyncMock(return_value=_session_row(ended_at=datetime.now(UTC)))
    )
    anthropic = _anthropic()
    with _patched(), pytest.raises(ConflictError, match="has ended"):
        await _service(repo, anthropic).send_events(
            _auth(),
            "ass_1",
            AgentEventSendRequest.model_validate(
                {"events": [{"type": "user.interrupt"}]}
            ),
        )

    anthropic.send_events.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "call,args",
    [
        ("send_events", (AgentEventSendRequest.model_validate(
            {"events": [{"type": "user.interrupt"}]},
        ),)),
        ("get_session", ()),
        ("list_events", ()),
        ("archive_session", ()),
    ],
)
async def test_someone_elses_session_is_a_404_on_every_route(
    call: str, args: tuple[Any, ...]
) -> None:
    """RLS returns no row rather than a forbidden one, and 404 is the right shape:
    telling them apart would leak that the session exists."""
    repo = _repo(get_session_row=AsyncMock(return_value=None))
    anthropic = _anthropic()
    with _patched(), pytest.raises(NotFoundError):
        await getattr(_service(repo, anthropic), call)(_auth(), "ass_theirs", *args)

    anthropic.send_events.assert_not_awaited()
    anthropic.list_events.assert_not_awaited()
    anthropic.archive_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_replay_proxy_forwards_the_cursor_and_the_after_bound() -> None:
    """§6.2's reconnect recipe: open the stream, then backfill the gap."""
    repo, anthropic = _repo(), _anthropic()
    with _patched():
        result = await _service(repo, anthropic).list_events(
            _auth(), "ass_1", limit=25, order="desc", page="cur_1", after="2026-09-04T00:00:00Z"
        )

    sent = anthropic.list_events.await_args
    assert sent.args[0] == "ses_remote_1"
    assert sent.kwargs["created_at_gt"] == "2026-09-04T00:00:00Z"
    assert sent.kwargs["page"] == "cur_1"
    # Forwarded verbatim — not reshaped, so a new vendor field survives.
    assert result == {"data": [], "next_page": None}


# ── archive ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_archive_stops_the_vendor_session_and_frees_the_slot() -> None:
    repo, anthropic = _repo(), _anthropic()
    with _patched():
        await _service(repo, anthropic).archive_session(_auth(), "ass_1")

    anthropic.archive_session.assert_awaited_once_with("ses_remote_1")
    written = repo.update_session_state.await_args.kwargs
    assert written["status"] == "terminated"
    assert written["ended"] is True


# ── webhook sync ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_unknown_session_is_not_an_error_and_costs_no_vendor_call() -> None:
    """One Anthropic org can serve several deployments of this app."""
    repo = _repo(resolve_session_tenant=AsyncMock(return_value=None))
    anthropic = _anthropic()
    with _patched():
        landed = await _service(repo, anthropic).sync_from_webhook(
            "ses_someone_elses", event_type="session.status_idled"
        )

    assert landed is False
    anthropic.get_session.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event_type,stop_reason,cleared,ended",
    [
        ("session.requires_action", "requires_action", False, False),
        ("session.status_idled", "end_turn", False, False),
        ("session.status_run_started", None, True, False),
        ("session.status_terminated", None, False, True),
    ],
)
async def test_each_webhook_type_folds_into_the_mirror(
    event_type: str, stop_reason: str | None, cleared: bool, ended: bool
) -> None:
    """The delivery carries only ``{id, type}``, so the type is the whole signal.

    ``requires_action`` is the one that earns its own mapping: an idle session
    waiting for a tool confirmation and an idle session that finished its turn
    are the same ``status`` and completely different things to render (§6.2).
    """
    repo, anthropic = _repo(), _anthropic()
    with _patched():
        landed = await _service(repo, anthropic).sync_from_webhook(
            "ses_remote_1", event_type=event_type
        )

    assert landed is True
    written = repo.update_session_state.await_args.kwargs
    assert written["stop_reason"] == stop_reason
    assert written["clear_stop_reason"] is cleared
    assert written["ended"] is ended
    # Usage is read back from the session, because the delivery has none.
    assert (written["input_tokens"], written["output_tokens"]) == (120, 45)


@pytest.mark.asyncio
async def test_the_webhook_writes_in_the_sessions_own_tenant() -> None:
    """It resolves the tenant across workspaces once, then scopes normally."""
    repo = _repo(
        resolve_session_tenant=AsyncMock(
            return_value={"id": "ass_9", "workspace_id": "wrk_9", "user_id": "usr_9"}
        )
    )
    opened: list[tuple[str, str]] = []

    def _run_in_tenant(_s: Any, workspace_id: str, user_id: str, *_rest: Any) -> Any:
        opened.append((workspace_id, user_id))
        return _AsyncCtx(_tenant())

    with patch.multiple(
        "app.modules.agents.sessions",
        get_tenant_session=MagicMock(return_value=_AsyncCtx(MagicMock())),
        get_session=MagicMock(return_value=_AsyncCtx(MagicMock())),
        run_in_tenant=MagicMock(side_effect=_run_in_tenant),
    ):
        await _service(repo, _anthropic()).sync_from_webhook(
            "ses_remote_1", event_type="session.status_idled"
        )

    assert opened == [("wrk_9", "usr_9")]
    assert repo.update_session_state.await_args.kwargs["workspace_id"] == "wrk_9"
    assert repo.update_session_state.await_args.kwargs["session_id"] == "ass_9"
