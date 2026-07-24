"""MCP ``query_brain`` credential resolution + throttling (Phase 5).

The MCP process listens on its own port with no slowapi limiter, so the per-IP
AUTH_LIMIT on unauthenticated key probes and the per-workspace BRAIN_LIMIT are
applied by hand in ``app/mcp/server.py``. These tests pin that: the throttle
runs *before* the key lookup (no DB hit for an exhausted window), and one IP's /
one workspace's budget is its own.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastmcp.exceptions import ToolError

from app.mcp import server as mcp_server
from app.shared.middleware import rate_limit
from app.shared.middleware.authenticate import AuthContext


class _FakeRedis:
    """Minimal in-memory stand-in for the fixed-window counters."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.ttls: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    async def expire(self, key: str, seconds: int) -> None:
        self.ttls[key] = seconds

    async def ttl(self, key: str) -> int:
        return self.ttls.get(key, -1)


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> _FakeRedis:
    fake = _FakeRedis()
    monkeypatch.setattr(rate_limit, "get_redis", lambda: fake)
    return fake


def _auth(scopes: list[str] | None = None, workspace_id: str = "wrk_1") -> AuthContext:
    return AuthContext(
        user_id="usr_1",
        workspace_id=workspace_id,
        role="viewer",
        scopes=scopes if scopes is not None else ["brain:query"],
        kind="api_key",
    )


def _headers(key: str | None = "hph_live_abc") -> dict[str, str]:
    return {"X-API-Key": key} if key else {}


def _patch_transport(ip: str = "203.0.113.7", key: str | None = "hph_live_abc"):
    """Stub the fastmcp HTTP-request accessors the tool reads."""
    request = type("_Req", (), {"client": type("_Client", (), {"host": ip})()})()
    return (
        patch.object(mcp_server, "get_http_headers", return_value=_headers(key)),
        patch.object(mcp_server, "get_http_request", return_value=request),
    )


@pytest.mark.asyncio
async def test_missing_api_key_is_rejected(fake_redis: _FakeRedis) -> None:
    headers, request = _patch_transport(key=None)
    with headers, request, pytest.raises(ToolError, match="missing X-API-Key"):
        await mcp_server._authenticate()
    # No key, no probe — the window is untouched.
    assert fake_redis.counts == {}


@pytest.mark.asyncio
async def test_invalid_api_key_is_rejected(fake_redis: _FakeRedis) -> None:
    headers, request = _patch_transport()
    with headers, request, patch.object(
        mcp_server, "authenticate_api_key", AsyncMock(return_value=None)
    ), pytest.raises(ToolError, match="invalid API key"):
        await mcp_server._authenticate()


@pytest.mark.asyncio
async def test_missing_scope_is_rejected(fake_redis: _FakeRedis) -> None:
    headers, request = _patch_transport()
    with headers, request, patch.object(
        mcp_server, "authenticate_api_key", AsyncMock(return_value=_auth(scopes=["skills:invoke"]))
    ), pytest.raises(ToolError, match="brain:query"):
        await mcp_server._authenticate()


@pytest.mark.asyncio
async def test_key_probes_are_throttled_per_ip_before_lookup(fake_redis: _FakeRedis) -> None:
    headers, request = _patch_transport()
    lookup = AsyncMock(return_value=None)
    with headers, request, patch.object(mcp_server, "authenticate_api_key", lookup):
        # AUTH_LIMIT is 10/minute: the first ten probes reach the key lookup.
        for _ in range(10):
            with pytest.raises(ToolError, match="invalid API key"):
                await mcp_server._authenticate()
        assert lookup.await_count == 10
        with pytest.raises(ToolError, match="Rate limited"):
            await mcp_server._authenticate()
        # The throttled probe short-circuits ahead of the DB.
        assert lookup.await_count == 10
    assert fake_redis.ttls["ratelimit:apikey:203.0.113.7"] == 60


@pytest.mark.asyncio
async def test_probe_throttle_is_per_ip(fake_redis: _FakeRedis) -> None:
    lookup = AsyncMock(return_value=None)
    headers, request = _patch_transport(ip="198.51.100.1")
    with headers, request, patch.object(mcp_server, "authenticate_api_key", lookup):
        for _ in range(11):
            with pytest.raises(ToolError):
                await mcp_server._authenticate()
    # A different peer starts from a fresh window and still reaches the lookup.
    headers, request = _patch_transport(ip="198.51.100.2")
    with headers, request, patch.object(mcp_server, "authenticate_api_key", lookup):
        with pytest.raises(ToolError, match="invalid API key"):
            await mcp_server._authenticate()


@pytest.mark.asyncio
async def test_query_brain_throttles_per_workspace(fake_redis: _FakeRedis) -> None:
    headers, request = _patch_transport()
    query = AsyncMock(return_value={"match_type": "exact", "cache_hit": True})
    with headers, request, patch.object(
        mcp_server, "authenticate_api_key", AsyncMock(return_value=_auth())
    ), patch.object(mcp_server._service, "query", query):
        # Both windows share the same 10 probes/minute ceiling on the auth side,
        # so drive the workspace limit with a fresh probe budget per call.
        for _ in range(10):
            assert await mcp_server.query_brain("how do refunds work?")
            fake_redis.counts.pop("ratelimit:apikey:203.0.113.7", None)
        assert query.await_count == 10
        assert fake_redis.counts["ratelimit:brain:wrk_1"] == 10


@pytest.mark.asyncio
async def test_query_brain_rejects_past_brain_limit(fake_redis: _FakeRedis) -> None:
    headers, request = _patch_transport()
    query = AsyncMock(return_value={"match_type": "exact", "cache_hit": True})
    # BRAIN_LIMIT is 120/minute — pre-fill the window to its ceiling.
    fake_redis.counts["ratelimit:brain:wrk_1"] = 120
    with headers, request, patch.object(
        mcp_server, "authenticate_api_key", AsyncMock(return_value=_auth())
    ), patch.object(mcp_server._service, "query", query):
        with pytest.raises(ToolError, match="Rate limited"):
            await mcp_server.query_brain("how do refunds work?")
    # The workspace's reads are cut off before the service runs.
    query.assert_not_awaited()


@pytest.mark.asyncio
async def test_brain_limit_is_per_workspace(fake_redis: _FakeRedis) -> None:
    headers, request = _patch_transport()
    query = AsyncMock(return_value={"match_type": "exact", "cache_hit": True})
    fake_redis.counts["ratelimit:brain:wrk_1"] = 120
    with headers, request, patch.object(
        mcp_server, "authenticate_api_key", AsyncMock(return_value=_auth(workspace_id="wrk_2"))
    ), patch.object(mcp_server._service, "query", query):
        assert await mcp_server.query_brain("how do refunds work?")
    assert query.await_count == 1


@pytest.mark.asyncio
async def test_client_ip_falls_back_when_transport_has_no_request() -> None:
    with patch.object(mcp_server, "get_http_request", side_effect=RuntimeError):
        assert mcp_server._client_ip() == "unknown"
