"""MCP server exposing ``query_brain`` to AI agents (Phase 5 — PRD §14, Feature 15).

Runs as its own process on ``settings.mcp_port`` (default 8001). Agents
authenticate with the **same** ``X-API-Key`` credential as the REST API: the tool
resolves it to a workspace-scoped ``AuthContext`` and requires the ``brain:query``
scope, **failing closed** — a missing/invalid key or missing scope raises before
any data is read, and there is no default workspace. All reads are RLS-scoped in
``SkillsService``.

Launch: ``python -m app.mcp.server`` (see the Dockerfile / compose ``mcp`` service).
"""
from __future__ import annotations

import asyncio
import logging

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers

from app.config.settings import settings
from app.modules.skills.service import SkillsService
from app.pipeline.query_extraction import run_query_extraction
from app.shared.middleware.authenticate import AuthContext, authenticate_api_key

log = logging.getLogger(__name__)

mcp: FastMCP = FastMCP("Company Brain")
_service = SkillsService()

_REQUIRED_SCOPE = "brain:query"


async def _authenticate() -> AuthContext:
    """Resolve the request's X-API-Key into a scoped context, or raise ToolError."""
    headers = {k.lower(): v for k, v in get_http_headers(include_all=True).items()}
    raw_key = headers.get("x-api-key")
    if not raw_key:
        raise ToolError("Unauthorized: missing X-API-Key header.")
    auth = await authenticate_api_key(raw_key)
    if auth is None:
        raise ToolError("Unauthorized: invalid API key.")
    if _REQUIRED_SCOPE not in set(auth.scopes):
        raise ToolError(f"Forbidden: API key is missing the '{_REQUIRED_SCOPE}' scope.")
    return auth


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
    result = await _service.query(auth, situation)
    if result["match_type"] == "no_match" and not result.get("cache_hit"):
        # Feature 16: escalate a genuine miss to inline query-driven extraction.
        return await run_query_extraction(auth, situation, prior=result)
    return result


async def run_mcp_server() -> None:
    """Serve the MCP tool over HTTP (SSE) on the configured port."""
    await mcp.run_http_async(transport="sse", host="0.0.0.0", port=settings.mcp_port)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_mcp_server())
