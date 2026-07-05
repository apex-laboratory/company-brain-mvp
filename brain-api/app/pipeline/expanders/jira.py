"""Jira expander: append an issue's comment thread (ADF-flattened) to its
summary + description.

API calls go through the Atlassian gateway ``.../ex/jira/{cloudId}/...``; the
cloudId is the connection's ``external_account_id`` (``req.account_id``). Comment
bodies are ADF (the same JSON tree the connector flattens for descriptions), so we
reuse the connector's ``_adf_to_text``.
"""
from __future__ import annotations

from app.integrations.base import http_client
from app.integrations.jira import _adf_to_text
from app.pipeline.expanders.base import ExpandRequest
from app.pipeline.types import ExpandedContext

_GATEWAY = "https://api.atlassian.com/ex/jira"


class JiraExpander:
    async def expand(self, req: ExpandRequest) -> ExpandedContext:
        cloud_id = req.account_id
        key = req.payload.get("key")
        if not cloud_id or not key:
            return ExpandedContext(text=req.content)

        resp = await http_client().get(
            f"{_GATEWAY}/{cloud_id}/rest/api/3/issue/{key}/comment",
            headers={"Authorization": f"Bearer {req.token}", "Accept": "application/json"},
            params={"maxResults": 100},
        )
        resp.raise_for_status()
        lines: list[str] = []
        for comment in resp.json().get("comments", []):
            author = (comment.get("author") or {}).get("displayName", "")
            body = _adf_to_text(comment.get("body")).strip()
            if body:
                lines.append(f"{author}: {body}" if author else body)

        text = req.content if not lines else f"{req.content}\n\n" + "\n\n".join(lines)
        return ExpandedContext(text=text, url=req.payload.get("_site_url", ""))
