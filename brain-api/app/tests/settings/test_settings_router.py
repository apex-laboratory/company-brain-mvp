"""Router contract tests for settings + API keys (KAN-64).

Drives the FastAPI app through an ASGI transport with the services and auth
context stubbed, asserting the response envelope, camelCase fields, the one-time
raw-key reveal, the 204 on revoke, request validation (unknown fields / bad
scopes), and the admin-only 403 for editor/viewer on key management and updates.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.modules.api_keys.schemas import ApiKeyCreated, ApiKeySummary
from app.modules.workspaces.schemas import (
    SettingsResponse,
    UpdatedWorkspace,
    UpdateSettingsResponse,
    WorkspaceConfig,
)
from app.shared.middleware.authenticate import AuthContext, get_auth_context

_NOW = datetime(2026, 6, 4, 10, 0, tzinfo=UTC)


# ── service stubs ─────────────────────────────────────────────────────────────
class _StubWorkspaceService:
    async def get_settings(self, auth: AuthContext, workspace_id: str) -> SettingsResponse:
        return SettingsResponse(
            workspace=WorkspaceConfig(
                name="Riverline", domain="riverline.io", plan="pro", seat_limit=12
            ),
            brain_endpoint="https://mcp.brainites.com/mcp",
        )

    async def update_settings(
        self, auth: AuthContext, workspace_id: str, fields: dict[str, str | None]
    ) -> UpdateSettingsResponse:
        return UpdateSettingsResponse(
            workspace=UpdatedWorkspace(
                id=workspace_id,
                name=str(fields.get("name", "Riverline")),
                domain=fields.get("domain", "riverline.io"),
            )
        )


class _StubApiKeyService:
    async def list_keys(self, auth: AuthContext, workspace_id: str) -> list[ApiKeySummary]:
        return [
            ApiKeySummary(
                id="key_123",
                name="Prod MCP",
                prefix="hph_live_abc1",
                scopes=["brain:query"],
                created_at=_NOW,
                last_used_at=_NOW,
            )
        ]

    async def create_key(self, auth: AuthContext, workspace_id: str, body: object) -> ApiKeyCreated:
        return ApiKeyCreated(
            id="key_123",
            name="Prod MCP",
            api_key="hph_live_" + "a" * 32,
            prefix="hph_live_aaaa",
            scopes=["brain:query", "skills:invoke"],
            created_at=_NOW,
        )

    async def revoke_key(self, auth: AuthContext, workspace_id: str, key_id: str) -> None:
        return None


def _set_auth(role: str) -> None:
    from app.main import app

    app.dependency_overrides[get_auth_context] = lambda: AuthContext(
        user_id="usr_1", workspace_id="wrk_1", role=role, scopes=[], kind="jwt"
    )


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from app.main import app
    from app.modules.api_keys.router import get_api_key_service
    from app.modules.workspaces.router import get_workspace_service
    from app.shared.middleware.rate_limit import limiter

    limiter.enabled = False  # avoid the Redis-backed limiter in unit tests
    _set_auth("admin")
    app.dependency_overrides[get_workspace_service] = lambda: _StubWorkspaceService()
    app.dependency_overrides[get_api_key_service] = lambda: _StubApiKeyService()

    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        limiter.enabled = True
        app.dependency_overrides.clear()


# ── settings ──────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_get_settings_envelope(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/workspaces/wrk_1/settings")

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["workspace"]["seatLimit"] == 12
    assert data["brainEndpoint"] == "https://mcp.brainites.com/mcp"


@pytest.mark.asyncio
async def test_patch_settings_updates_fields(client: AsyncClient) -> None:
    resp = await client.patch(
        "/api/v1/workspaces/wrk_1/settings", json={"name": "Riverline 2"}
    )

    assert resp.status_code == 200
    assert resp.json()["data"]["workspace"]["name"] == "Riverline 2"


@pytest.mark.asyncio
async def test_patch_settings_rejects_unknown_field(client: AsyncClient) -> None:
    resp = await client.patch(
        "/api/v1/workspaces/wrk_1/settings", json={"slug": "hacked"}
    )

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


@pytest.mark.asyncio
async def test_patch_settings_forbidden_for_editor(client: AsyncClient) -> None:
    _set_auth("editor")
    resp = await client.patch(
        "/api/v1/workspaces/wrk_1/settings", json={"name": "X"}
    )

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


# ── api keys ──────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_list_api_keys_returns_prefix_only(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/workspaces/wrk_1/api-keys")

    assert resp.status_code == 200
    item = resp.json()["data"][0]
    assert item["prefix"] == "hph_live_abc1"
    assert "apiKey" not in item
    assert item["lastUsedAt"] is not None


@pytest.mark.asyncio
async def test_create_api_key_reveals_raw_key_201(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/workspaces/wrk_1/api-keys",
        json={"name": "Prod MCP", "scopes": ["brain:query", "skills:invoke"]},
    )

    assert resp.status_code == 201
    data = resp.json()["data"]
    assert data["apiKey"].startswith("hph_live_")
    assert data["prefix"] == "hph_live_aaaa"


@pytest.mark.asyncio
async def test_create_api_key_rejects_bad_scope(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/workspaces/wrk_1/api-keys",
        json={"name": "Prod", "scopes": ["wat:nope"]},
    )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_api_key_forbidden_for_viewer(client: AsyncClient) -> None:
    _set_auth("viewer")
    resp = await client.post(
        "/api/v1/workspaces/wrk_1/api-keys",
        json={"name": "Prod", "scopes": ["brain:query"]},
    )

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_revoke_api_key_returns_204(client: AsyncClient) -> None:
    resp = await client.delete("/api/v1/workspaces/wrk_1/api-keys/key_123")

    assert resp.status_code == 204
    assert resp.content == b""


@pytest.mark.asyncio
async def test_revoke_api_key_forbidden_for_editor(client: AsyncClient) -> None:
    _set_auth("editor")
    resp = await client.delete("/api/v1/workspaces/wrk_1/api-keys/key_123")

    assert resp.status_code == 403
