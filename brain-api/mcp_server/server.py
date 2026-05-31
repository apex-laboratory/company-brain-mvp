from fastmcp import FastMCP

mcp = FastMCP("Company Brain")


@mcp.tool()
async def query_brain(situation: str) -> dict:
    """
    Query the company brain for the operational skill that matches this situation.
    Call this before executing any company-specific task.

    Returns the skill's trigger, base decision logic, exceptions table,
    available actions, confidence score, source authority, version, and
    match metadata (match_type, similarity_score).

    If no published skill matches (similarity < 0.70), triggers a live
    extraction from connected sources and returns the result flagged as
    match_type=query_driven.
    """
    return {
        "status": "stub",
        "message": "MCP server running. Skill retrieval implemented in Phase 5.",
        "situation_received": situation,
    }


async def run_mcp_server():
    await mcp.run_http_async(transport="sse", host="0.0.0.0", port=8001)
