"""Cross-tenant guards for every agent-builder path (phase 1's definition of done).

RLS is the real isolation, and these tests never reach Postgres — so what they
can prove is the half that lives in our code: **every repository call and every
``run_in_tenant`` is scoped with the caller's own workspace, never a value from
the path or body.** That is the input RLS depends on. A method that passed a
request-supplied workspace would defeat the policy without failing any of the
behavioural tests, which is exactly why this is swept per-method rather than
spot-checked once.

The other half — that Postgres actually refuses the row — needs the integration
suite and a live database, and is not covered here or anywhere yet.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules.agents.schemas import (
    AgentConnectorCreateRequest,
    AgentCreateRequest,
    AgentUpdateRequest,
)
from app.modules.agents.service import AgentsService
from app.tests.agents.test_agents import _AsyncCtx, _auth, _row
from app.tests.agents.test_connectors import _connector

# The caller's workspace, from the auth context. Anything else reaching a
# repository call or run_in_tenant is a cross-tenant leak.
_CALLER_WORKSPACE = "wrk_1"
# A workspace the caller does not belong to, used as the "attacker" value in the
# paths that take an id from the URL.
_OTHER_WORKSPACE = "wrk_2"


def _repo() -> MagicMock:
    repo = MagicMock()
    repo.get = AsyncMock(return_value=_row())
    repo.list_visible = AsyncMock(return_value=[])
    repo.insert = AsyncMock(return_value=_row())
    repo.update = AsyncMock(return_value=_row())
    repo.set_visibility = AsyncMock(return_value=_row())
    # One existing connector, named so it does not collide with the one
    # ``add_connector`` declares below — and with the id ``remove_connector``
    # is asked to remove, so both paths reach their repository calls.
    repo.list_connectors = AsyncMock(
        return_value=[_connector(id="acn_1", name="already-there")]
    )
    repo.insert_connector = AsyncMock(return_value=_connector())
    repo.delete_connector = AsyncMock(return_value=True)
    return repo


def _anthropic() -> MagicMock:
    anthropic = MagicMock()
    anthropic.create_agent = AsyncMock(return_value={"id": "agent_r", "version": 1})
    anthropic.update_agent = AsyncMock(return_value={"id": "agent_r", "version": 2})
    anthropic.list_versions = AsyncMock(return_value=[])
    return anthropic


async def _run(call: str, *args: Any, **kwargs: Any) -> tuple[MagicMock, list[str]]:
    """Invoke one service method, capturing every workspace it scoped with."""
    repo, anthropic = _repo(), _anthropic()
    service = AgentsService(repository=repo, anthropic=anthropic)
    tenant = MagicMock()
    tenant.commit = AsyncMock()
    opened: list[str] = []

    def _run_in_tenant(_s: Any, workspace_id: str, *_rest: Any) -> Any:
        opened.append(workspace_id)
        return _AsyncCtx(tenant)

    with patch(
        "app.modules.agents.service.get_tenant_session",
        return_value=_AsyncCtx(MagicMock()),
    ), patch("app.modules.agents.service.run_in_tenant", side_effect=_run_in_tenant):
        await getattr(service, call)(_auth(), *args, **kwargs)
    return repo, opened


def _bound_workspaces(repo: MagicMock) -> set[str]:
    """Every ``workspace_id=`` keyword the service bound into repository calls."""
    found: set[str] = set()
    for name in dir(repo):
        if name.startswith("_"):
            continue
        method = getattr(repo, name)
        for call in getattr(method, "await_args_list", []) or []:
            if "workspace_id" in call.kwargs:
                found.add(call.kwargs["workspace_id"])
    return found


# Every service method, with arguments that carry an id from the "path". The
# agent and connector ids are deliberately foreign-looking: none of them may
# influence which workspace is scoped.
_CALLS: list[tuple[str, tuple[Any, ...]]] = [
    ("list_agents", ()),
    ("get_agent", ("agt_from_path",)),
    ("create_agent", (AgentCreateRequest(name="Refunds"),)),
    ("update_agent", ("agt_from_path", AgentUpdateRequest(name="Renamed"))),
    ("list_versions", ("agt_from_path",)),
    ("list_connectors", ("agt_from_path",)),
    (
        "add_connector",
        (
            "agt_from_path",
            AgentConnectorCreateRequest(
                name="linear", mcp_server_url="https://mcp.linear.app/mcp"
            ),
        ),
    ),
    ("remove_connector", ("agt_from_path", "acn_1")),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("call,args", _CALLS, ids=[c for c, _ in _CALLS])
async def test_every_repository_call_is_bound_to_the_callers_workspace(
    call: str, args: tuple[Any, ...]
) -> None:
    repo, _ = await _run(call, *args)
    bound = _bound_workspaces(repo)
    assert bound, f"{call} bound no workspace_id at all — nothing scopes it"
    assert bound == {_CALLER_WORKSPACE}, (
        f"{call} scoped a repository call to {bound - {_CALLER_WORKSPACE}} "
        "instead of the caller's workspace"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("call,args", _CALLS, ids=[c for c, _ in _CALLS])
async def test_every_transaction_opens_in_the_callers_workspace(
    call: str, args: tuple[Any, ...]
) -> None:
    """``run_in_tenant`` sets the Postgres session variables RLS reads. Opening
    one with a foreign workspace would hand the policy the wrong tenant."""
    _, opened = await _run(call, *args)
    assert opened, f"{call} opened no tenant transaction"
    assert set(opened) == {_CALLER_WORKSPACE}


@pytest.mark.asyncio
@pytest.mark.parametrize("visibility", ["workspace", "private"])
async def test_set_visibility_is_scoped_too(visibility: str) -> None:
    """Publish takes its own path through the repository, so it is swept
    separately rather than trusted to look like the others."""
    repo, opened = await _run(
        "set_visibility", "agt_from_path", visibility=visibility
    )
    assert _bound_workspaces(repo) == {_CALLER_WORKSPACE}
    assert set(opened) == {_CALLER_WORKSPACE}


@pytest.mark.asyncio
async def test_a_foreign_workspace_never_appears_anywhere() -> None:
    """Belt and braces: the sweep above asserts equality, this asserts that the
    specific value an attacker would supply cannot show up."""
    for call, args in _CALLS:
        repo, opened = await _run(call, *args)
        assert _OTHER_WORKSPACE not in _bound_workspaces(repo), call
        assert _OTHER_WORKSPACE not in opened, call


# ── credentials and vaults (phase 2) ──────────────────────────────────────────
#
# The credential paths have a second tenancy question the agent paths do not.
# For a dashboard call the answer is the same one swept above — the workspace
# comes from the auth context. But the **OAuth callback carries no auth context
# at all**: it is an unauthenticated browser redirect, and the only thing that
# says which workspace and which user this token belongs to is the signed,
# single-use state row. So the guard there is that the workspace comes from the
# consumed state and from nowhere else — not the path, not the query string.


def _credentials_repo() -> MagicMock:
    from app.tests.agents.test_credentials import _credential_row, _vault_row

    repo = MagicMock()
    repo.list_credentials = AsyncMock(return_value=[])
    repo.list_unauthorized_connectors = AsyncMock(return_value=[])
    repo.get_credential = AsyncMock(return_value=_credential_row())
    repo.get_credential_for_server = AsyncMock(return_value=None)
    repo.upsert_credential = AsyncMock(return_value=_credential_row())
    repo.delete_credential_row = AsyncMock(return_value=True)
    repo.get_vault = AsyncMock(return_value=_vault_row())
    repo.insert_vault = AsyncMock(return_value=_vault_row())
    repo.create_agent_oauth_state = AsyncMock(return_value=None)
    return repo


async def _run_credentials(call: str, *args: Any) -> tuple[MagicMock, list[str]]:
    from app.modules.agents.credentials import AgentCredentialsService

    repo = _credentials_repo()
    service = AgentCredentialsService(repository=repo, anthropic=_credentials_anthropic())
    tenant = MagicMock()
    tenant.commit = AsyncMock()
    opened: list[str] = []

    def _run_in_tenant(_s: Any, workspace_id: str, *_rest: Any) -> Any:
        opened.append(workspace_id)
        return _AsyncCtx(tenant)

    with patch.multiple(
        "app.modules.agents.credentials",
        get_tenant_session=MagicMock(return_value=_AsyncCtx(MagicMock())),
        get_session=MagicMock(return_value=_AsyncCtx(MagicMock())),
        run_in_tenant=MagicMock(side_effect=_run_in_tenant),
    ):
        await getattr(service, call)(_auth(), *args)
    return repo, opened


def _credentials_anthropic() -> MagicMock:
    client = MagicMock()
    client.create_vault = AsyncMock(return_value="vlt_new")
    client.create_mcp_oauth_credential = AsyncMock(return_value="cred_new")
    client.delete_credential = AsyncMock(return_value=None)
    return client


_CREDENTIAL_CALLS: list[tuple[str, tuple[Any, ...]]] = [
    ("overview", ()),
    ("list_catalog", ()),
    ("disconnect", ("acr_from_path",)),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "call,args", _CREDENTIAL_CALLS, ids=[c for c, _ in _CREDENTIAL_CALLS]
)
async def test_credential_reads_and_writes_are_bound_to_the_callers_workspace(
    call: str, args: tuple[Any, ...]
) -> None:
    repo, opened = await _run_credentials(call, *args)
    bound = _bound_workspaces(repo)
    assert bound == {_CALLER_WORKSPACE}, f"{call} scoped to {bound}"
    assert set(opened) == {_CALLER_WORKSPACE}
    assert _OTHER_WORKSPACE not in bound and _OTHER_WORKSPACE not in opened


@pytest.mark.asyncio
async def test_credential_writes_are_bound_to_the_calling_user_too() -> None:
    """Unlike agents, these rows are *personal*: ``agent_credentials``' policy
    compares ``user_id`` to ``current_user_id()``, so a call that bound another
    member's id would write a row its own caller could not read back."""
    users: set[str] = set()
    for call, args in _CREDENTIAL_CALLS:
        repo, _ = await _run_credentials(call, *args)
        for name in dir(repo):
            if name.startswith("_"):
                continue
            for made in getattr(getattr(repo, name), "await_args_list", []) or []:
                if "user_id" in made.kwargs and made.kwargs["user_id"] is not None:
                    users.add(made.kwargs["user_id"])
    assert users == {"usr_1"}


@pytest.mark.asyncio
async def test_the_callback_takes_its_tenant_from_the_state_not_the_request() -> None:
    """The callback is unauthenticated. If the workspace could come from anywhere
    the browser controls, a forged redirect would write a credential into
    somebody else's tenant."""
    from app.modules.agents import oauth
    from app.modules.agents.credentials import AgentCredentialsService
    from app.tests.agents.test_credentials import _SPEC, _grant, _state

    repo = _credentials_repo()
    repo.consume_agent_oauth_state = AsyncMock(
        return_value={
            "user_id": "usr_from_state",
            "workspace_id": _OTHER_WORKSPACE,  # the state's workspace, not the caller's
            "redirect_uri": "https://api.test/cb",
            "return_to": None,
            "frontend_origin": None,
        }
    )
    service = AgentCredentialsService(
        repository=repo, anthropic=_credentials_anthropic()
    )
    opened: list[str] = []

    def _run_in_tenant(_s: Any, workspace_id: str, *_rest: Any) -> Any:
        opened.append(workspace_id)
        return _AsyncCtx(MagicMock(commit=AsyncMock()))

    async def _exchange(*_a: Any, **_k: Any) -> Any:
        return _grant()

    with patch.multiple(
        "app.modules.agents.credentials",
        get_tenant_session=MagicMock(return_value=_AsyncCtx(MagicMock())),
        get_session=MagicMock(return_value=_AsyncCtx(MagicMock())),
        run_in_tenant=MagicMock(side_effect=_run_in_tenant),
        _require_spec=MagicMock(return_value=_SPEC),
    ), patch.object(oauth, "exchange_code", side_effect=_exchange):
        await service.handle_callback("provider", state=_state(), code="c")

    assert set(opened) == {_OTHER_WORKSPACE}
    assert _bound_workspaces(repo) == {_OTHER_WORKSPACE}
    assert repo.upsert_credential.await_args.kwargs["user_id"] == "usr_from_state"
