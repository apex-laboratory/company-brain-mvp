"""MCP server exposing ``query_brain`` to AI agents (Phase 5 — PRD §14, Feature 15).

Runs as its own process on ``settings.mcp_port`` (default 8001) over **Streamable
HTTP** — the transport Anthropic Managed Agents connects on. SSE is the deprecated
MCP transport and is deliberately *not* dual-mounted: this is the only
internet-facing service carrying an API key, and a second transport would mean a
second auth path and a second rate-limit surface (see ``docs/agent-builder-plan.md``
§4.1).

Agents authenticate with the same credential as the REST API, presented either as
``X-API-Key: <key>`` or ``Authorization: Bearer <key>`` — a vault ``static_bearer``
credential arrives in the latter form. Both resolve through ``authenticate_api_key``
to a workspace-scoped ``AuthContext`` requiring the ``brain:query`` scope, **failing
closed** — a missing/invalid key or missing scope raises before any data is read, and
there is no default workspace. All reads are RLS-scoped in ``SkillsService``.

Launch: ``python -m app.mcp.server`` (see the Dockerfile / compose ``mcp`` service).
"""
from __future__ import annotations

import asyncio
import logging

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers, get_http_request

from app.config.redis import close_redis, init_redis
from app.config.settings import settings
from app.modules.skills.service import SkillsService
from app.pipeline.query_extraction import run_query_extraction
from app.shared.errors.app_error import RateLimitError
from app.shared.middleware.authenticate import AuthContext, authenticate_api_key
from app.shared.middleware.rate_limit import (
    enforce_api_key_probe_limit,
    enforce_brain_query_limit,
)

log = logging.getLogger(__name__)

mcp: FastMCP = FastMCP("Company Brain")
_service = SkillsService()

_REQUIRED_SCOPE = "brain:query"


def _client_ip() -> str:
    """Best-effort peer address for rate-limit keying.

    Falls back to a shared ``unknown`` bucket when the transport exposes no HTTP
    request (fails closed: unattributable probes throttle each other rather than
    bypassing the limit).
    """
    try:
        request = get_http_request()
    except RuntimeError:
        return "unknown"
    return request.client.host if request.client else "unknown"


def _presented_key(headers: dict[str, str]) -> str | None:
    """The API key from ``X-API-Key`` or an ``Authorization: Bearer`` header.

    Managed Agents injects a vault ``static_bearer`` credential as a Bearer token,
    so the same key reaches us in two shapes. ``X-API-Key`` wins when both are
    present. Only the ``Bearer`` scheme is read: a JWT presented here would fail
    ``authenticate_api_key`` anyway, so agent credentials stay the one way in.
    """
    raw_key = headers.get("x-api-key")
    if raw_key:
        return raw_key
    authorization = headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        return token.strip()
    return None


async def _authenticate() -> AuthContext:
    """Resolve the request's API key into a scoped context, or raise ToolError.

    Mirrors the REST auth dependency, throttle included: this process listens on
    its own port with no slowapi limiter, so the per-IP AUTH_LIMIT is applied
    here — *before* the key lookup — or key hashes could be brute-forced against
    an unthrottled surface.
    """
    headers = {k.lower(): v for k, v in get_http_headers(include_all=True).items()}
    raw_key = _presented_key(headers)
    if not raw_key:
        raise ToolError(
            "Unauthorized: missing credential. Send X-API-Key or Authorization: Bearer."
        )
    try:
        await enforce_api_key_probe_limit(_client_ip())
    except RateLimitError as exc:
        raise ToolError(_rate_limited_message(exc)) from exc
    auth = await authenticate_api_key(raw_key)
    if auth is None:
        raise ToolError("Unauthorized: invalid API key.")
    if _REQUIRED_SCOPE not in set(auth.scopes):
        raise ToolError(f"Forbidden: API key is missing the '{_REQUIRED_SCOPE}' scope.")
    return auth


def _rate_limited_message(exc: RateLimitError) -> str:
    retry_after = exc.details.get("retryAfterSeconds") if exc.details else None
    suffix = f" Retry after {retry_after}s." if retry_after else ""
    return f"Rate limited: too many requests.{suffix}"


@mcp.tool()
async def query_brain(situation: str) -> dict:
    """Query the company brain for the operational skill matching this situation.

    Call this before executing any company-specific task. Returns the matched
    skill's trigger, base decision logic, exceptions table, available actions,
    confidence, source authority, version, and match metadata (``match_type``,
    ``similarity_score``), plus an ``interaction_id`` you can pass to
    ``POST /interactions/{id}/override`` if the agent overrides the guidance.

    If no published skill matches (similarity < 0.70), a live extraction runs from
    connected sources within a 15-second budget and the result is returned flagged
    ``match_type="query_driven"``; if the budget is exceeded the response is
    ``match_type="no_match"`` with ``extraction_queued=true`` and a
    ``retry_after_seconds`` hint — retry then or escalate to a human.
    """
    auth = await _authenticate()
    # BRAIN_LIMIT parity with the REST brain routes (which get it from the
    # slowapi decorator); a query can escalate to a live LLM extraction.
    try:
        await enforce_brain_query_limit(str(auth.workspace_id))
    except RateLimitError as exc:
        raise ToolError(_rate_limited_message(exc)) from exc
    result = await _service.query(auth, situation)
    if (
        result["match_type"] == "no_match"
        and not result.get("cache_hit")
        and not auth.agent_origin
    ):
        # Feature 16: escalate a genuine miss to inline query-driven extraction.
        # Skipped for agent-origin credentials: extraction would write the raw
        # situation text (which may quote connector or file contents) into a
        # skill and the query log without human review. See agent-builder-plan §4.4.
        return await run_query_extraction(auth, situation, prior=result)
    return result


async def run_mcp_server() -> None:
    """Serve the MCP tool over Streamable HTTP on the configured port.

    This process runs no FastAPI lifespan, so it initializes the shared Redis
    client itself — the rate limiter above depends on it.
    """
    await init_redis()
    try:
        await mcp.run_http_async(transport="http", host="0.0.0.0", port=settings.mcp_port)
    finally:
        await close_redis()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_mcp_server())
