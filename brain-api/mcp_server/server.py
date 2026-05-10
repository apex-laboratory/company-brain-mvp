from fastmcp import FastMCP

mcp = FastMCP("Company Brain")


@mcp.tool()
async def query_brain(situation: str, entities: dict = {}) -> dict:
    """
    Query the company brain for the operational skill that matches this situation.
    Call this before executing any company-specific task.
    Returns decision logic, tool schemas, confidence score, and graph_context
    showing resolved overrides and dependencies.
    """
    return {
        "status": "stub",
        "message": "MCP server running. Skill retrieval implemented in Phase 5.",
        "situation_received": situation,
    }


async def run_mcp_server():
    await mcp.run_http_async(transport="sse", host="0.0.0.0", port=8001)
