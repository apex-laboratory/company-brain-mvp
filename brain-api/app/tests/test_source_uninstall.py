"""Provider-side uninstall on disconnect.

Most connectors end access by revoking a token. A GitHub App can't: the
installation outlives every token it mints, so disconnecting used to delete our
row and leave the App sitting on the user's repositories.

The guard these tests exist for: one installation can be connected in several
workspaces (``resolve_all_by_account`` fans webhooks out to all of them), so
uninstalling on the *first* disconnect would silently kill the others' access.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.modules.sources import service as service_module
from app.modules.sources.service import SourcesService
from app.shared.helpers.crypto import encrypt
from app.shared.middleware.authenticate import AuthContext


class _AsyncCtx:
    def __init__(self, value: object) -> None:
        self._value = value

    async def __aenter__(self) -> object:
        return self._value

    async def __aexit__(self, *_: object) -> bool:
        return False


def _auth() -> AuthContext:
    return AuthContext(user_id="usr_1", workspace_id="wrk_1", role="admin", scopes=[], kind="jwt")


_SECRETS = {
    "id": "src_1",
    "provider": "github",
    "access_token_enc": encrypt("tok").encode(),
    "refresh_token_enc": None,
    "token_expires_at": None,
    "external_account_id": "151024022",
}


async def _disconnect(
    *,
    others: int,
    integration: MagicMock,
    secrets: dict | None = None,
) -> MagicMock:
    """Run disconnect with ``others`` sibling connections to the same account."""
    repo = MagicMock(
        get_connection_secrets=AsyncMock(return_value=secrets if secrets is not None else _SECRETS),
        count_other_connections_for_account=AsyncMock(return_value=others),
        revoke_subscriptions_for_source=AsyncMock(),
        delete_connection=AsyncMock(return_value=True),
    )
    svc = SourcesService(repository=repo, sweeps_service=MagicMock())
    session = MagicMock(commit=AsyncMock())
    with patch.object(
        service_module, "get_tenant_session", return_value=_AsyncCtx(session)
    ), patch.object(
        service_module, "get_session", return_value=_AsyncCtx(session)
    ), patch.object(
        service_module, "run_in_tenant", return_value=_AsyncCtx(None)
    ), patch.object(service_module, "get_integration", return_value=integration):
        await svc.disconnect(_auth(), "src_1")
    return repo


def _github(uninstall: AsyncMock | None = None) -> MagicMock:
    integration = MagicMock(revoke=AsyncMock(), uninstall=uninstall or AsyncMock())
    return integration


def _tokenful_provider() -> MagicMock:
    """A connector with no `uninstall` capability (Notion/Slack/Google/…)."""
    integration = MagicMock(revoke=AsyncMock())
    del integration.uninstall  # MagicMock auto-creates attributes; force absence
    return integration


@pytest.mark.asyncio
async def test_last_connection_uninstalls_the_app() -> None:
    uninstall = AsyncMock()
    repo = await _disconnect(others=0, integration=_github(uninstall))
    uninstall.assert_awaited_once_with("151024022")
    repo.delete_connection.assert_awaited_once()


@pytest.mark.asyncio
async def test_another_workspace_still_connected_keeps_the_installation() -> None:
    # The whole reason for the guard: workspace B must keep working after A leaves.
    uninstall = AsyncMock()
    repo = await _disconnect(others=1, integration=_github(uninstall))
    uninstall.assert_not_awaited()
    repo.delete_connection.assert_awaited_once()  # local disconnect still happens


@pytest.mark.asyncio
async def test_connector_without_uninstall_is_untouched() -> None:
    # Notion/Slack/Google revoke a token and have nothing to uninstall; they must not
    # be required to declare the capability, and must not trigger the account count.
    integration = _tokenful_provider()
    repo = await _disconnect(others=0, integration=integration)
    integration.revoke.assert_awaited_once()
    repo.count_other_connections_for_account.assert_not_awaited()


@pytest.mark.asyncio
async def test_connection_without_account_id_is_skipped() -> None:
    # No installation id means nothing to address the uninstall to.
    secrets = {**_SECRETS, "external_account_id": None}
    repo = await _disconnect(others=0, integration=_github(), secrets=secrets)
    repo.count_other_connections_for_account.assert_not_awaited()
    repo.delete_connection.assert_awaited_once()


@pytest.mark.asyncio
async def test_uninstall_failure_still_disconnects_locally() -> None:
    # GitHub being down must not strand a connection the user asked to remove; the
    # leftover installation is visible and removable from GitHub's own settings.
    uninstall = AsyncMock(side_effect=httpx.HTTPError("boom"))
    repo = await _disconnect(others=0, integration=_github(uninstall))
    repo.delete_connection.assert_awaited_once()


@pytest.mark.asyncio
async def test_account_count_excludes_the_connection_being_removed() -> None:
    # Off-by-one guard: counting the row we're about to delete would make every
    # disconnect look like "someone else still has it" and never uninstall.
    repo = await _disconnect(others=0, integration=_github())
    kwargs = repo.count_other_connections_for_account.await_args.kwargs
    assert kwargs["excluding_source_id"] == "src_1"
    assert kwargs["external_account_id"] == "151024022"
    assert kwargs["provider"] == "github"
