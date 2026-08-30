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


def _auth(
    scopes: list[str] | None = None,
    workspace_id: str = "wrk_1",
    agent_origin: bool = False,
) -> AuthContext:
    return AuthContext(
        user_id="usr_1",
        workspace_id=workspace_id,
        role="viewer",
        scopes=scopes if scopes is not None else ["brain:query"],
        kind="api_key",
        agent_origin=agent_origin,
    )


def _headers(
    key: str | None = "hph_live_abc", scheme: str = "x-api-key"
) -> dict[str, str]:
    if not key:
        return {}
    if scheme == "bearer":
        return {"Authorization": f"Bearer {key}"}
    return {"X-API-Key": key}


def _patch_transport(
    ip: str = "203.0.113.7",
    key: str | None = "hph_live_abc",
    scheme: str = "x-api-key",
):
    """Stub the fastmcp HTTP-request accessors the tool reads."""
    request = type("_Req", (), {"client": type("_Client", (), {"host": ip})()})()
    return (
        patch.object(mcp_server, "get_http_headers", return_value=_headers(key, scheme)),
        patch.object(mcp_server, "get_http_request", return_value=request),
    )


@pytest.mark.asyncio
async def test_missing_api_key_is_rejected(fake_redis: _FakeRedis) -> None:
    headers, request = _patch_transport(key=None)
    with headers, request, pytest.raises(ToolError, match="missing credential"):
        await mcp_server._authenticate()
    # No key, no probe — the window is untouched.
    assert fake_redis.counts == {}


@pytest.mark.asyncio
async def test_bearer_credential_is_accepted(fake_redis: _FakeRedis) -> None:
    """A vault ``static_bearer`` credential arrives as Authorization: Bearer."""
    headers, request = _patch_transport(scheme="bearer")
    lookup = AsyncMock(return_value=_auth())
    with headers, request, patch.object(mcp_server, "authenticate_api_key", lookup):
        auth = await mcp_server._authenticate()
    assert auth.workspace_id == "wrk_1"
    # Resolved through the same key path, so the scope check is unchanged.
    lookup.assert_awaited_once_with("hph_live_abc")


@pytest.mark.asyncio
async def test_bearer_credential_still_requires_the_scope(fake_redis: _FakeRedis) -> None:
    headers, request = _patch_transport(scheme="bearer")
    with headers, request, patch.object(
        mcp_server, "authenticate_api_key",
        AsyncMock(return_value=_auth(scopes=["skills:invoke"])),
    ), pytest.raises(ToolError, match="brain:query"):
        await mcp_server._authenticate()


def test_x_api_key_wins_when_both_headers_are_present() -> None:
    presented = mcp_server._presented_key(
        {"x-api-key": "from_header", "authorization": "Bearer from_bearer"}
    )
    assert presented == "from_header"


def test_non_bearer_authorization_scheme_is_ignored() -> None:
    assert mcp_server._presented_key({"authorization": "Basic abc123"}) is None
    assert mcp_server._presented_key({"authorization": "Bearer   "}) is None
    assert mcp_server._presented_key({}) is None


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
async def test_miss_escalates_to_extraction_for_a_dashboard_credential(
    fake_redis: _FakeRedis,
) -> None:
    headers, request = _patch_transport()
    query = AsyncMock(return_value={"match_type": "no_match", "cache_hit": False})
    extract = AsyncMock(return_value={"match_type": "no_match", "extraction_queued": True})
    with headers, request, patch.object(
        mcp_server, "authenticate_api_key", AsyncMock(return_value=_auth())
    ), patch.object(mcp_server._service, "query", query), patch.object(
        mcp_server, "run_query_extraction", extract
    ):
        await mcp_server.query_brain("how do refunds work?")
    extract.assert_awaited_once()


@pytest.mark.asyncio
async def test_agent_origin_miss_never_reaches_query_extraction(
    fake_redis: _FakeRedis,
) -> None:
    """The back door: extraction writes a skill from the raw query text.

    An agent's query may quote a Slack DM or a file it just read, so an
    agent-origin credential gets the honest no_match and no extraction.
    """
    headers, request = _patch_transport()
    query = AsyncMock(return_value={"match_type": "no_match", "cache_hit": False})
    extract = AsyncMock()
    with headers, request, patch.object(
        mcp_server, "authenticate_api_key",
        AsyncMock(return_value=_auth(agent_origin=True)),
    ), patch.object(mcp_server._service, "query", query), patch.object(
        mcp_server, "run_query_extraction", extract
    ):
        result = await mcp_server.query_brain("refund for the customer in this DM")
    extract.assert_not_awaited()
    # Still a real answer, just never an extracted one.
    assert result["match_type"] == "no_match"


@pytest.mark.asyncio
async def test_client_ip_falls_back_when_transport_has_no_request() -> None:
    with patch.object(mcp_server, "get_http_request", side_effect=RuntimeError):
        assert mcp_server._client_ip() == "unknown"
