"""Notion expander: fetch a page's block children so the extractor sees the whole
document, not just the page title/metadata the sync stored.

Recurses one level into nested blocks (toggles, list children) with a bounded
block budget — deep pages contribute their top structure without unbounded fanout.
"""
from __future__ import annotations

from app.integrations.base import http_client
from app.integrations.notion import _NOTION_VERSION
from app.pipeline.expanders.base import ExpandRequest
from app.pipeline.types import ExpandedContext

_API = "https://api.notion.com/v1"
_MAX_BLOCKS = 200  # cap total blocks fetched per page (bounds recursion cost)
# _NOTION_VERSION is imported from the integration so the pinned API version can't
# drift between the sync and the expander.


def _rich_text(block: dict) -> str:
    """Extract plain text from a block's type-specific ``rich_text`` array."""
    body = block.get(block.get("type", ""), {})
    if not isinstance(body, dict):
        return ""
    return "".join(rt.get("plain_text", "") for rt in body.get("rich_text", []))


class NotionExpander:
    def _headers(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}", "Notion-Version": _NOTION_VERSION}

    async def _children(self, block_id: str, token: str, budget: list[int]) -> list[str]:
        if budget[0] <= 0:
            return []
        resp = await http_client().get(
            f"{_API}/blocks/{block_id}/children",
            headers=self._headers(token),
            params={"page_size": 100},
        )
        resp.raise_for_status()
        lines: list[str] = []
        for block in resp.json().get("results", []):
            budget[0] -= 1
            if budget[0] <= 0:
                break
            text = _rich_text(block)
            if text:
                lines.append(text)
            if block.get("has_children") and budget[0] > 0:
                lines.extend(await self._children(block["id"], token, budget))
        return lines

    async def expand(self, req: ExpandRequest) -> ExpandedContext:
        page_id = req.payload.get("id")
        if not page_id:
            return ExpandedContext(text=req.content)
        lines = await self._children(page_id, req.token, [_MAX_BLOCKS])
        body = "\n".join(lines)
        # Prepend the normalized content (page title) so the page is self-describing.
        text = f"{req.content}\n\n{body}".strip() if body else req.content
        return ExpandedContext(text=text, url=req.payload.get("url", ""))
