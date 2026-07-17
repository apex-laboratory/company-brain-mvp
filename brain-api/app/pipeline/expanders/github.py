"""GitHub expander: append an issue/PR's discussion (comments) to its body.

The decision on a PR or issue usually lands in the comment thread, not the opening
description. We follow the ``comments_url`` GitHub already put on the item, so this
works for both issues and PRs without reconstructing the repo/number.
"""
from __future__ import annotations

from app.integrations.base import http_client
from app.integrations.github import _BASE_HEADERS
from app.pipeline.expanders.base import ExpandRequest
from app.pipeline.types import ExpandedContext

# Reuse the integration's pinned API headers so an API-version bump reaches the
# expander too (they must stay in lock-step).
_HEADERS = _BASE_HEADERS


def _core(payload: dict) -> dict:
    """The issue/PR object, whether the payload is a webhook envelope or bare item."""
    return payload.get("issue") or payload.get("pull_request") or payload


class GitHubExpander:
    async def expand(self, req: ExpandRequest) -> ExpandedContext:
        core = _core(req.payload)
        comments_url = core.get("comments_url")
        if not comments_url:
            return ExpandedContext(text=req.content)

        resp = await http_client().get(
            comments_url,
            headers={"Authorization": f"Bearer {req.token}", **_HEADERS},
            params={"per_page": 100},
        )
        resp.raise_for_status()
        participants: list[dict] = []
        lines: list[str] = []
        for comment in resp.json():
            author = (comment.get("user") or {}).get("login", "")
            body = (comment.get("body") or "").strip()
            if body:
                lines.append(f"{author}: {body}" if author else body)
            if author:
                participants.append({"id": author})

        text = req.content if not lines else f"{req.content}\n\n" + "\n\n".join(lines)
        return ExpandedContext(
            text=text, participants=participants, url=core.get("html_url", "")
        )
