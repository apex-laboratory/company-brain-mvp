"""Service unit tests for the API key module (KAN-64).

Drives ``ApiKeyService`` against a fake repository with the DB session and tenant
context stubbed, asserting key format + one-time reveal, that only the hash (not
the raw key) reaches the repository, the list never carries a secret, 403 for
non-members, and 404 when revoking an unknown key.
"""
from __future__ import annotations

import contextlib
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest

from app.modules.api_keys import service as service_module
from app.modules.api_keys.repository import ApiKeyRepository, ApiKeyRow
from app.modules.api_keys.schemas import ApiKeyCreateRequest
from app.modules.api_keys.service import ApiKeyService
from app.shared.errors.app_error import ForbiddenError, NotFoundError
from app.shared.helpers.crypto import sha256_hash
from app.shared.middleware.authenticate import AuthContext

_NOW = datetime(2026, 6, 4, 10, 0, tzinfo=UTC)


def _auth(workspace_id: str | None = "wrk_1") -> AuthContext:
    return AuthContext(
        user_id="usr_1", workspace_id=workspace_id, role="admin", scopes=[], kind="jwt"
    )


class _FakeRepo(ApiKeyRepository):
    def __init__(self, *, keys: list[ApiKeyRow] | None = None, revoke: bool = True) -> None:
        self._keys = keys or []
        self._revoke = revoke
        self.created: dict[str, Any] | None = None
        self.revoked: tuple[str, str] | None = None

    async def list_keys(self, session: Any, workspace_id: str) -> list[ApiKeyRow]:
        return self._keys

    async def create_key(self, session: Any, **kwargs: Any) -> datetime:
        self.created = kwargs
        return _NOW

    async def revoke_key(self, session: Any, workspace_id: str, key_id: str) -> bool:
        self.revoked = (workspace_id, key_id)
        return self._revoke


class _FakeSession:
    async def commit(self) -> None:  # pragma: no cover - trivial
        pass


@pytest.fixture(autouse=True)
def _stub_session_and_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    @contextlib.asynccontextmanager
    async def fake_tenant_session(*_args: Any, **_kwargs: Any) -> AsyncIterator[_FakeSession]:
        yield _FakeSession()

    monkeypatch.setattr(service_module, "tenant_session", fake_tenant_session)


@pytest.mark.asyncio
async def test_create_reveals_raw_key_once_and_stores_only_hash() -> None:
    repo = _FakeRepo()
    service = ApiKeyService(repository=repo)
    body = ApiKeyCreateRequest(name="Prod MCP", scopes=["brain:query", "skills:invoke"])

    result = await service.create_key(_auth(), "wrk_1", body)

    # Raw key format: hph_live_<32 hex>.
    assert re.fullmatch(r"hph_live_[0-9a-f]{32}", result.api_key)
    # Prefix is hph_live_ + first 4 of the random part, and the raw key starts with it.
    assert result.prefix.startswith("hph_live_")
    assert result.api_key.startswith(result.prefix)
    assert result.id.startswith("key_")
    assert result.scopes == ["brain:query", "skills:invoke"]

    # The repository receives the hash of the raw key — never the raw key itself.
    assert repo.created is not None
    assert repo.created["key_hash"] == sha256_hash(result.api_key)
    assert repo.created["key_prefix"] == result.prefix
    assert repo.created["created_by"] == "usr_1"
    assert "api_key" not in repo.created


@pytest.mark.asyncio
async def test_list_exposes_prefix_not_secret() -> None:
    repo = _FakeRepo(
        keys=[
            ApiKeyRow(
                id="key_1",
                name="Prod",
                prefix="hph_live_abc1",
                scopes=["brain:query"],
                created_at=_NOW,
                last_used_at=_NOW,
            )
        ]
    )
    service = ApiKeyService(repository=repo)

    keys = await service.list_keys(_auth(), "wrk_1")

    assert len(keys) == 1
    dumped = keys[0].model_dump(by_alias=True)
    assert dumped["prefix"] == "hph_live_abc1"
    assert "apiKey" not in dumped and "keyHash" not in dumped


@pytest.mark.asyncio
async def test_non_member_forbidden_on_create() -> None:
    service = ApiKeyService(repository=_FakeRepo())
    body = ApiKeyCreateRequest(name="X", scopes=["brain:query"])

    with pytest.raises(ForbiddenError):
        await service.create_key(_auth(workspace_id="wrk_OTHER"), "wrk_1", body)


@pytest.mark.asyncio
async def test_revoke_unknown_key_maps_to_404() -> None:
    service = ApiKeyService(repository=_FakeRepo(revoke=False))

    with pytest.raises(NotFoundError):
        await service.revoke_key(_auth(), "wrk_1", "key_missing")


@pytest.mark.asyncio
async def test_revoke_passes_workspace_and_key() -> None:
    repo = _FakeRepo(revoke=True)
    service = ApiKeyService(repository=repo)

    await service.revoke_key(_auth(), "wrk_1", "key_1")

    assert repo.revoked == ("wrk_1", "key_1")
