"""Google Drive expander: append a document's comment thread to its body.

The Drive connector already exports the full document text into ``_content`` at
sync time, so the body is present — the expander adds the comment discussion
(``files/{id}/comments``), where review decisions on a doc typically live.
"""
from __future__ import annotations

from app.integrations.base import http_client
from app.pipeline.expanders.base import ExpandRequest
from app.pipeline.types import ExpandedContext

_API = "https://www.googleapis.com/drive/v3"


class GoogleDriveExpander:
    async def expand(self, req: ExpandRequest) -> ExpandedContext:
        file_id = req.payload.get("id")
        if not file_id:
            return ExpandedContext(text=req.content)

        resp = await http_client().get(
            f"{_API}/files/{file_id}/comments",
            headers={"Authorization": f"Bearer {req.token}"},
            params={"fields": "comments(content,author/displayName)", "pageSize": 100},
        )
        resp.raise_for_status()
        lines: list[str] = []
        for comment in resp.json().get("comments", []):
            author = (comment.get("author") or {}).get("displayName", "")
            body = (comment.get("content") or "").strip()
            if body:
                lines.append(f"{author}: {body}" if author else body)

        if not lines:
            return ExpandedContext(text=req.content)
        return ExpandedContext(text=f"{req.content}\n\n" + "\n\n".join(lines))
