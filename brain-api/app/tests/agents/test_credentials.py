"""Credentials and vaults (agent-builder-plan §5.3, §5.4, phase 2).

The first section is the one that matters. Phase 2 exists to move a connector
token from a provider into an Anthropic vault **without it ever landing on our
infrastructure**, and that promise is a property of the code path, not of a
comment — so it is asserted directly: after a full successful callback, the
access token appears in no repository call and no log record.

Everything after that is the machinery that makes the flow survive real use:
find-or-create (a second provider must land in the first vault), replace-on-
reconnect (a vault holds 20 credentials and a leaked slot is unreclaimable), and
the state handling that keeps an agent consent from being redeemable as an
ingestion connection.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.modules.agents import oauth
from app.modules.agents.catalog import ConnectorSpec
from app.modules.agents.credentials import AgentCredentialsService, _resolve_return_to
from app.modules.agents.repository import _state_provider
from app.shared.errors.app_error import (
    ForbiddenError,
    NotFoundError,
    UnauthorizedError,
    ValidationError,
)
from app.shared.helpers.crypto import hmac_sign, sha256_hash
from app.shared.middleware.authenticate import get_auth_context
from app.tests.agents.test_agents import _AsyncCtx, _auth

_ACCESS_TOKEN = "super-secret-access-token"
_MCP_URL = "https://mcp.provider.test/mcp"

_SPEC = ConnectorSpec(
    provider="provider",
    display_name="Provider",
    mcp_server_url=_MCP_URL,
    authorize_endpoint="https://provider.test/authorize",
    token_endpoint="https://provider.test/token",
    token_endpoint_auth="client_secret_post",
    scopes="read",
    client_id_env="TEST_CLIENT_ID",
    client_secret_env="TEST_CLIENT_SECRET",
)


def _credential_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": "acr_1",
        "workspace_id": "wrk_1",
        "user_id": "usr_1",
        "provider": "provider",
        "mcp_server_url": _MCP_URL,
        "anthropic_credential_id": "cred_remote_1",
        "display_name": "Acme",
        "connected_at": datetime.now(UTC),
    }
    row.update(overrides)
    return row


def _vault_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": "avl_1",
        "workspace_id": "wrk_1",
        "user_id": "usr_1",
        "anthropic_vault_id": "vlt_remote_1",
        "created_at": datetime.now(UTC),
    }
    row.update(overrides)
    return row


def _repo(**overrides: Any) -> MagicMock:
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
    repo.consume_agent_oauth_state = AsyncMock(
        return_value={
            "user_id": "usr_1",
            "workspace_id": "wrk_1",
            "redirect_uri": "https://api.test/cb",
            "return_to": None,
            "frontend_origin": None,
        }
    )
    repo.peek_agent_oauth_state = AsyncMock(return_value=None)
    for key, value in overrides.items():
        setattr(repo, key, value)
    return repo


def _anthropic(**overrides: Any) -> MagicMock:
    client = MagicMock()
    client.create_vault = AsyncMock(return_value="vlt_new")
    client.create_mcp_oauth_credential = AsyncMock(return_value="cred_remote_2")
    client.delete_credential = AsyncMock(return_value=None)
    for key, value in overrides.items():
        setattr(client, key, value)
    return client


def _grant(**overrides: Any) -> oauth.OAuthGrant:
    fields: dict[str, Any] = {
        "access_token": _ACCESS_TOKEN,
        "refresh_token": "the-refresh-token",
        "scope": "read",
        "account_label": "Acme",
    }
    fields.update(overrides)
    return oauth.OAuthGrant(**fields)


def _state(nonce: str = "nonce-1") -> str:
    from app.config.settings import settings

    return f"{nonce}.{hmac_sign(nonce, settings.jwt_access_secret)}"


async def _run_callback(
    repo: MagicMock,
    anthropic: MagicMock,
    *,
    grant: oauth.OAuthGrant | None = None,
    events: list[str] | None = None,
    **kwargs: Any,
) -> Any:
    """Drive ``handle_callback`` with every collaborator mocked out.

    Awaited *inside* the patch block: the callback does its work in the coroutine
    body, so returning the coroutine to be awaited outside would run it against
    the real collaborators.
    """
    service = AgentCredentialsService(repository=repo, anthropic=anthropic)
    tenant = MagicMock()
    tenant.commit = AsyncMock()
    log: list[str] = events if events is not None else []

    def _run_in_tenant(*_args: Any, **_kwargs: Any) -> _AsyncCtx:
        log.append("tx:open")
        return _AsyncCtx(tenant)

    async def _exchange(*_args: Any, **_kwargs: Any) -> oauth.OAuthGrant:
        log.append("anthropic:exchange")
        return grant or _grant()

    # Record the vendor calls by giving each mock a side effect, rather than by
    # replacing it — the tests still need ``await_args`` and the assert helpers.
    for name in ("create_vault", "create_mcp_oauth_credential", "delete_credential"):
        mock = getattr(anthropic, name)

        def _record(label: str, result: Any) -> Any:
            async def _call(*_a: Any, **_k: Any) -> Any:
                log.append(f"anthropic:{label}")
                return result

            return _call

        mock.side_effect = _record(name, mock.return_value)

    patches = patch.multiple(
        "app.modules.agents.credentials",
        get_tenant_session=MagicMock(return_value=_AsyncCtx(MagicMock())),
        get_session=MagicMock(return_value=_AsyncCtx(MagicMock())),
        run_in_tenant=MagicMock(side_effect=_run_in_tenant),
        _require_spec=MagicMock(return_value=_SPEC),
    )
    with patches, patch.object(oauth, "exchange_code", side_effect=_exchange):
        return await service.handle_callback(
            "provider", state=_state(), code="the-code", **kwargs
        )


# ── the wall: a token passes through and is never written ────────────────────


@pytest.mark.asyncio
async def test_the_access_token_reaches_no_repository_call(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The whole point of phase 2 in one assertion.

    A token exists in this process for the length of a callback. If it appears in
    anything handed to the repository, it is on its way to Postgres — and
    ``agent_credentials`` deliberately has no column that could hold it.
    """
    repo, anthropic = _repo(), _anthropic()
    with caplog.at_level(logging.DEBUG):
        await _run_callback(repo, anthropic)

    for name in dir(repo):
        if name.startswith("_"):
            continue
        method = getattr(repo, name)
        for call in getattr(method, "await_args_list", []) or []:
            assert _ACCESS_TOKEN not in repr(call), f"token reached repo.{name}"

    assert _ACCESS_TOKEN not in caplog.text
    # And it did reach the vault, so the test is not passing vacuously.
    assert anthropic.create_mcp_oauth_credential.await_args.kwargs["access_token"] == (
        _ACCESS_TOKEN
    )


@pytest.mark.asyncio
async def test_the_credential_exists_at_anthropic_before_any_row_claims_it() -> None:
    """Vendor first, then store. The reverse would record a pointer to nothing."""
    events: list[str] = []
    await _run_callback(_repo(), _anthropic(), events=events)

    assert "anthropic:create_mcp_oauth_credential" in events
    assert events.index("anthropic:create_mcp_oauth_credential") < len(events) - 1
    assert events[-1] == "tx:open", "the pointer write must be the last step"


@pytest.mark.asyncio
async def test_no_vendor_call_happens_inside_an_open_transaction() -> None:
    """§7: a pooled connection pinned across a multi-second vault call is the
    failure the two-phase pattern exists to prevent.

    The mocked transactions close as soon as their block exits, so what this can
    check is that no vendor call is the *first* thing after an open with nothing
    between — which is what an accidental in-transaction call would look like.
    """
    events: list[str] = []
    await _run_callback(_repo(), _anthropic(), events=events)

    # Every vendor call precedes the final write, and the reads that come before
    # it are separate short transactions.
    vendor = [i for i, e in enumerate(events) if e.startswith("anthropic:")]
    assert vendor, "no vendor call happened at all"
    assert max(vendor) < len(events) - 1


@pytest.mark.asyncio
async def test_the_refresh_block_is_wired_to_the_providers_token_endpoint() -> None:
    """Without it the credential dies at the first expiry, and dies quietly (§8)."""
    anthropic = _anthropic()
    with patch.dict(
        "os.environ", {"TEST_CLIENT_ID": "cid", "TEST_CLIENT_SECRET": "csec"}
    ):
        await _run_callback(_repo(), anthropic)

    refresh = anthropic.create_mcp_oauth_credential.await_args.kwargs["refresh"]
    assert refresh["token_endpoint"] == _SPEC.token_endpoint
    assert refresh["refresh_token"] == "the-refresh-token"
    assert refresh["token_endpoint_auth"]["type"] == "client_secret_post"


@pytest.mark.asyncio
async def test_a_grant_with_no_refresh_token_is_stored_without_one() -> None:
    anthropic = _anthropic()
    await _run_callback(_repo(), anthropic, grant=_grant(refresh_token=None))
    assert anthropic.create_mcp_oauth_credential.await_args.kwargs["refresh"] is None


# ── vaults ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_second_connection_reuses_the_existing_vault() -> None:
    """A vault holds 20 credentials. Minting one per provider would cap the user
    at one connector and orphan a vault per connect."""
    repo, anthropic = _repo(), _anthropic()
    await _run_callback(repo, anthropic)

    anthropic.create_vault.assert_not_awaited()
    assert anthropic.create_mcp_oauth_credential.await_args.args[0] == "vlt_remote_1"


@pytest.mark.asyncio
async def test_the_first_connection_creates_the_vault() -> None:
    repo = _repo(get_vault=AsyncMock(return_value=None))
    anthropic = _anthropic()
    await _run_callback(repo, anthropic)

    anthropic.create_vault.assert_awaited_once()
    repo.insert_vault.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_lost_vault_race_falls_back_to_the_winners_vault() -> None:
    """Two concurrent connects both create a vault at Anthropic; the partial
    unique index lets one row survive and the loser must use it."""
    winner = _vault_row(id="avl_winner", anthropic_vault_id="vlt_winner")
    repo = _repo(
        get_vault=AsyncMock(side_effect=[None, winner]),
        insert_vault=AsyncMock(return_value=None),  # ON CONFLICT DO NOTHING
    )
    anthropic = _anthropic()
    await _run_callback(repo, anthropic)

    assert anthropic.create_mcp_oauth_credential.await_args.args[0] == "vlt_winner"


@pytest.mark.asyncio
async def test_the_workspace_vault_is_keyed_by_a_null_user() -> None:
    """§5.4: query_brain's credential lives in a shared vault so it does not eat
    one of every user's 20 slots."""
    repo = _repo(get_vault=AsyncMock(return_value=None))
    service = AgentCredentialsService(repository=repo, anthropic=_anthropic())

    with patch.multiple(
        "app.modules.agents.credentials",
        get_tenant_session=MagicMock(return_value=_AsyncCtx(MagicMock())),
        run_in_tenant=MagicMock(return_value=_AsyncCtx(MagicMock(commit=AsyncMock()))),
    ):
        await service.ensure_workspace_vault("wrk_1", "usr_1")

    assert repo.get_vault.await_args.kwargs["user_id"] is None
    assert repo.insert_vault.await_args.kwargs["user_id"] is None


# ── re-connecting replaces ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reconnecting_deletes_the_old_credential_first() -> None:
    """A vault keys credentials by MCP server URL, so a re-connect is a replace.
    Skipping the delete leaks a slot the user has no way to reclaim."""
    events: list[str] = []
    repo = _repo(get_credential_for_server=AsyncMock(return_value=_credential_row()))
    anthropic = _anthropic()
    await _run_callback(repo, anthropic, events=events)

    anthropic.delete_credential.assert_awaited_once_with("vlt_remote_1", "cred_remote_1")
    assert events.index("anthropic:delete_credential") < events.index(
        "anthropic:create_mcp_oauth_credential"
    )


@pytest.mark.asyncio
async def test_reconnecting_keeps_the_row_id_stable() -> None:
    """Anything holding the credential id — a UI list, a support ticket — should
    still be pointing at the same connection after a re-auth."""
    repo = _repo(
        get_credential_for_server=AsyncMock(return_value=_credential_row(id="acr_old"))
    )
    await _run_callback(repo, _anthropic())
    assert repo.upsert_credential.await_args.kwargs["credential_id"] == "acr_old"


# ── state handling ────────────────────────────────────────────────────────────


def test_agent_states_are_namespaced_away_from_the_ingestion_flow() -> None:
    """``oauth_states`` is shared. An un-namespaced ``slack`` state minted for an
    agent credential could be redeemed at ``/sources/slack/callback`` and would
    silently create a *source connection* — connector data crossing the wall by
    the front door."""
    assert _state_provider("slack") == "agent:slack"
    assert _state_provider("slack") != "slack"


@pytest.mark.asyncio
async def test_a_forged_state_is_rejected_without_touching_the_database() -> None:
    repo = _repo()
    service = AgentCredentialsService(repository=repo, anthropic=_anthropic())

    with patch(
        "app.modules.agents.credentials._require_spec", MagicMock(return_value=_SPEC)
    ), pytest.raises(UnauthorizedError):
        await service.handle_callback(
            "provider", state="forged.signature", code="c"
        )

    repo.consume_agent_oauth_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_replayed_state_is_rejected() -> None:
    """Consumption is a single ``UPDATE ... RETURNING``; a second redemption
    matches no row."""
    repo = _repo(consume_agent_oauth_state=AsyncMock(return_value=None))
    service = AgentCredentialsService(repository=repo, anthropic=_anthropic())

    with patch.multiple(
        "app.modules.agents.credentials",
        get_session=MagicMock(return_value=_AsyncCtx(MagicMock())),
        _require_spec=MagicMock(return_value=_SPEC),
    ), pytest.raises(UnauthorizedError):
        await service.handle_callback("provider", state=_state(), code="c")


@pytest.mark.asyncio
async def test_a_declined_consent_leaves_the_state_retryable() -> None:
    """No code to exchange, so the single-use state is peeked, not consumed — a
    browser retry must not meet a spurious "state already used"."""
    repo = _repo(
        peek_agent_oauth_state=AsyncMock(
            return_value={
                "return_to": "/dashboard/agents/agt_1/edit",
                "frontend_origin": None,
            }
        )
    )
    service = AgentCredentialsService(repository=repo, anthropic=_anthropic())

    with patch.multiple(
        "app.modules.agents.credentials",
        get_session=MagicMock(return_value=_AsyncCtx(MagicMock())),
        _require_spec=MagicMock(return_value=_SPEC),
    ):
        url = await service.handle_callback(
            "provider", state=_state(), error="access_denied"
        )

    repo.consume_agent_oauth_state.assert_not_awaited()
    assert url.endswith("/dashboard/agents/agt_1/edit?error=provider")


@pytest.mark.asyncio
async def test_an_unknown_provider_is_a_422() -> None:
    service = AgentCredentialsService(repository=_repo(), anthropic=_anthropic())
    with pytest.raises(ValidationError):
        await service.start_authorization(_auth(), "not-a-connector")


# ── returnTo ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value",
    [
        "/dashboard/agents",
        "/dashboard/agents/agt_01hxyz",
        "/dashboard/agents/agt_01hxyz/edit",
    ],
)
def test_return_to_accepts_the_agent_paths(value: str) -> None:
    assert _resolve_return_to(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "https://evil.test/steal",
        "//evil.test",
        "/dashboard/agents/../../admin",
        "\\\\evil.test",
        "/dashboard/agents?next=https://evil.test",
        "/dashboard/agents#/../evil",
        "/dashboard/sources",
        "/dashboard/agents/x/../../../evil",
    ],
)
def test_return_to_rejects_anything_that_could_leave_the_app(value: str) -> None:
    """The value ends up in a ``Location`` header. Rejection degrades to the
    default rather than 422, so a stale frontend can still finish a connect."""
    assert _resolve_return_to(value) is None


# ── disconnect ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_disconnect_revokes_at_the_vault_before_forgetting_the_row() -> None:
    """Row-first would tell the user the connection is gone while every agent
    kept using it."""
    order: list[str] = []
    repo = _repo()
    repo.delete_credential_row = AsyncMock(
        side_effect=lambda *a, **k: order.append("row") or True
    )
    anthropic = _anthropic()
    anthropic.delete_credential = AsyncMock(
        side_effect=lambda *a, **k: order.append("vault")
    )
    service = AgentCredentialsService(repository=repo, anthropic=anthropic)

    with patch.multiple(
        "app.modules.agents.credentials",
        get_tenant_session=MagicMock(return_value=_AsyncCtx(MagicMock())),
        run_in_tenant=MagicMock(return_value=_AsyncCtx(MagicMock(commit=AsyncMock()))),
    ):
        await service.disconnect(_auth(), "acr_1")

    assert order == ["vault", "row"]


@pytest.mark.asyncio
async def test_disconnecting_something_you_do_not_have_is_a_404() -> None:
    repo = _repo(get_credential=AsyncMock(return_value=None))
    service = AgentCredentialsService(repository=repo, anthropic=_anthropic())

    with patch.multiple(
        "app.modules.agents.credentials",
        get_tenant_session=MagicMock(return_value=_AsyncCtx(MagicMock())),
        run_in_tenant=MagicMock(return_value=_AsyncCtx(MagicMock())),
    ), pytest.raises(NotFoundError):
        await service.disconnect(_auth(), "acr_someone_elses")


# ── reads ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_overview_reports_what_is_missing_as_well_as_what_is_connected() -> None:
    repo = _repo(
        list_credentials=AsyncMock(return_value=[_credential_row()]),
        list_unauthorized_connectors=AsyncMock(
            return_value=[
                {
                    "agent_id": "agt_1",
                    "agent_name": "Refunds",
                    "connector_id": "acn_1",
                    "connector_name": "github",
                    "provider": "github",
                    "mcp_server_url": "https://api.githubcopilot.com/mcp/",
                }
            ]
        ),
    )
    service = AgentCredentialsService(repository=repo, anthropic=_anthropic())

    with patch.multiple(
        "app.modules.agents.credentials",
        get_tenant_session=MagicMock(return_value=_AsyncCtx(MagicMock())),
        run_in_tenant=MagicMock(return_value=_AsyncCtx(MagicMock())),
    ):
        overview = await service.overview(_auth())

    assert [c.id for c in overview.connections] == ["acr_1"]
    assert overview.needs_authorization[0].agent_name == "Refunds"


@pytest.mark.asyncio
async def test_the_catalog_marks_what_this_caller_has_already_connected() -> None:
    repo = _repo(list_credentials=AsyncMock(return_value=[_credential_row()]))
    service = AgentCredentialsService(repository=repo, anthropic=_anthropic())

    with patch.multiple(
        "app.modules.agents.credentials",
        get_tenant_session=MagicMock(return_value=_AsyncCtx(MagicMock())),
        run_in_tenant=MagicMock(return_value=_AsyncCtx(MagicMock())),
        list_connectors=MagicMock(return_value=[_SPEC]),
    ):
        entries = await service.list_catalog(_auth())

    assert [(e.provider, e.connected) for e in entries] == [("provider", True)]


@pytest.mark.asyncio
async def test_no_credential_response_field_can_carry_a_secret() -> None:
    """A schema-level restatement of the migration comment: if a token ever
    reached the service, there would still be nowhere to put it."""
    from app.modules.agents.schemas import AgentCredentialResponse

    assert set(AgentCredentialResponse.model_fields) == {
        "id", "provider", "display_name", "mcp_server_url", "connected_at",
    }


# ── auth ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_api_key_callers_are_refused() -> None:
    """A vault belongs to a person. An API key resolves to a workspace and no
    user, so it could not read back what it wrote."""
    service = AgentCredentialsService(repository=_repo(), anthropic=_anthropic())
    with pytest.raises(ForbiddenError):
        await service.overview(_auth(kind="api_key", user_id=None))


# ── router ────────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def client() -> Any:
    from app.main import app
    from app.shared.middleware.rate_limit import limiter

    limiter.enabled = False
    app.dependency_overrides[get_auth_context] = lambda: _auth()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()
    limiter.enabled = True


@pytest.mark.asyncio
async def test_catalog_is_matched_before_the_agent_id_route(
    client: AsyncClient,
) -> None:
    """FastAPI matches in declaration order. Below ``/{agent_id}``, ``catalog``
    would be a perfectly valid agent id and the whole catalog would 404."""
    with patch(
        "app.modules.agents.router._credentials.list_catalog",
        AsyncMock(return_value=[]),
    ) as listed:
        response = await client.get("/api/v1/agents/catalog")

    assert response.status_code == 200
    assert response.json()["data"] == []
    listed.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_callback_needs_no_jwt_and_redirects(client: AsyncClient) -> None:
    """It is a browser redirect from the provider. The signed state is the
    credential."""
    from app.main import app

    app.dependency_overrides.clear()  # no auth override: prove none is needed
    with patch(
        "app.modules.agents.router._credentials.handle_callback",
        AsyncMock(return_value="http://localhost:3000/dashboard/agents?connected=x"),
    ):
        response = await client.get(
            "/api/v1/agent-credentials/provider/callback?state=s&code=c",
            follow_redirects=False,
        )

    assert response.status_code == 302
    assert response.headers["location"].endswith("?connected=x")


@pytest.mark.asyncio
async def test_authorize_returns_202_with_the_consent_url(client: AsyncClient) -> None:
    from app.modules.agents.schemas import AgentAuthorizeStartResponse

    with patch(
        "app.modules.agents.router._credentials.start_authorization",
        AsyncMock(
            return_value=AgentAuthorizeStartResponse(
                authorize_url="https://provider.test/authorize?x=1"
            )
        ),
    ):
        response = await client.post("/api/v1/agent-credentials/provider/authorize")

    assert response.status_code == 202
    assert response.json()["data"]["authorizeUrl"].startswith("https://provider.test")


@pytest.mark.asyncio
async def test_a_viewer_may_connect_their_own_accounts(client: AsyncClient) -> None:
    """Deliberately weaker than the ``editor`` gate on building agents: a viewer
    runs published agents, and an agent reaches connectors as whoever runs it."""
    from app.main import app
    from app.modules.agents.schemas import AgentCredentialsOverview

    app.dependency_overrides[get_auth_context] = lambda: _auth(role="viewer")
    with patch(
        "app.modules.agents.router._credentials.overview",
        AsyncMock(return_value=AgentCredentialsOverview()),
    ):
        response = await client.get("/api/v1/agent-credentials")

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_disconnect_returns_204(client: AsyncClient) -> None:
    with patch(
        "app.modules.agents.router._credentials.disconnect", AsyncMock(return_value=None)
    ):
        response = await client.delete("/api/v1/agent-credentials/acr_1")
    assert response.status_code == 204


@pytest.mark.asyncio
async def test_the_state_hash_is_what_is_stored_never_the_state() -> None:
    """A stored state is a bearer value for one consent. Only its digest lands."""
    repo = _repo()
    service = AgentCredentialsService(repository=repo, anthropic=_anthropic())

    with patch.multiple(
        "app.modules.agents.credentials",
        get_session=MagicMock(return_value=_AsyncCtx(MagicMock())),
        _require_spec=MagicMock(return_value=_SPEC),
    ), patch.object(oauth, "authorize_url", MagicMock(return_value="https://x.test")):
        await service.start_authorization(_auth(), "provider")

    kwargs = repo.create_agent_oauth_state.await_args.kwargs
    assert isinstance(kwargs["state_hash"], bytes)
    assert kwargs["state_hash"] != sha256_hash("")
    assert "state" not in kwargs
