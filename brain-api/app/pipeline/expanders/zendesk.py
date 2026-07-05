"""Zendesk expander: append a ticket's comment chain (the actual resolution) to
its subject/description.

The subdomain rides on the event payload (``_subdomain``, injected by the Zendesk
connector's ``fetch_since``); the token is the connection's OAuth token.
"""
from __future__ import annotations

from app.integrations.base import http_client
from app.pipeline.expanders.base import ExpandRequest
from app.pipeline.types import ExpandedContext


class ZendeskExpander:
    async def expand(self, req: ExpandRequest) -> ExpandedContext:
        subdomain = req.payload.get("_subdomain", "")
        ticket_id = req.payload.get("id")
        if not subdomain or ticket_id is None:
            return ExpandedContext(text=req.content)

        url = f"https://{subdomain}.zendesk.com/api/v2/tickets/{ticket_id}/comments.json"
        resp = await http_client().get(
            url,
            headers={"Authorization": f"Bearer {req.token}"},
            params={"page[size]": 100},
        )
        resp.raise_for_status()
        lines: list[str] = []
        for comment in resp.json().get("comments", []):
            body = (comment.get("plain_body") or comment.get("body") or "").strip()
            if body:
                author = comment.get("author_id", "")
                lines.append(f"{author}: {body}" if author else body)

        text = req.content if not lines else f"{req.content}\n\n" + "\n\n".join(lines)
        tags = req.payload.get("tags") or []
        return ExpandedContext(text=text, metadata={"tags": tags})
