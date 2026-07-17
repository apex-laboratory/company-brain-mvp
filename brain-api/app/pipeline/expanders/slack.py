"""Slack expander: pull the full thread around a message (``conversations.replies``).

A single Slack message is rarely the whole decision — the reasoning lives in the
replies. We fetch the thread, join every reply as ``author: text``, and surface
reactions/pins as authority signals for the decision identifier.
"""
from __future__ import annotations

from app.integrations.base import http_client
from app.pipeline.expanders.base import ExpandRequest
from app.pipeline.types import ExpandedContext

_API = "https://slack.com/api/conversations.replies"


def _event(payload: dict) -> dict:
    if payload.get("type") == "event_callback":
        return payload.get("event") or {}
    return payload


class SlackExpander:
    async def expand(self, req: ExpandRequest) -> ExpandedContext:
        event = _event(req.payload)
        channel = event.get("channel", "")
        thread_ts = event.get("thread_ts") or event.get("ts")
        if not channel or not thread_ts:
            return ExpandedContext(text=req.content)

        resp = await http_client().get(
            _API,
            headers={"Authorization": f"Bearer {req.token}"},
            params={"channel": channel, "ts": thread_ts, "limit": 100},
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            # ok:false (e.g. thread_not_found) → degrade to the single message.
            return ExpandedContext(text=req.content)

        messages = data.get("messages") or []
        lines: list[str] = []
        reactions: list[dict] = []
        participants: list[dict] = []
        for msg in messages:
            author = msg.get("user", "")
            text = (msg.get("text") or "").strip()
            if text:
                lines.append(f"{author}: {text}" if author else text)
            if author:
                participants.append({"id": author})
            for r in msg.get("reactions") or []:
                reactions.append({"name": r.get("name"), "count": r.get("count", 0)})

        return ExpandedContext(
            text="\n".join(lines) or req.content,
            participants=participants,
            reactions=reactions,
            metadata={"pinned": bool(messages and messages[0].get("pinned_to"))},
        )
