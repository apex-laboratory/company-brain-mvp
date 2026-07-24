"""Native title of a source item, per provider (BRAIN_CHAT_RAG_PLAN — citations).

The evidence citation should name the source **document** the way its provider does
— the Jira summary, GitHub PR/issue title, Drive filename, Gmail subject, Notion
page title — not just the policy name. Each is pulled from the raw item stored in
``source_events.payload``. Best-effort: a shape we don't recognize (or Slack, whose
messages have no title) returns ``None`` and the caller falls back to the policy
title, so the label is always something and the link is always exact.
"""
from __future__ import annotations

import logging

from app.integrations.notion import _title_of as _notion_title

log = logging.getLogger(__name__)


def document_title(provider: str | None, payload: dict | None) -> str | None:
    """The source item's own title from its raw ``payload`` (``None`` if none)."""
    if not payload:
        return None
    try:
        if provider == "notion":
            return _clean(_notion_title(payload))
        if provider == "jira":
            fields = payload.get("fields") or {}
            summary = (fields.get("summary") or "").strip()
            key = (payload.get("key") or "").strip()
            if summary:
                return _clean(f"{key}: {summary}" if key else summary)
            return _clean(key)
        if provider == "github":
            return _clean(_github_title(payload))
        if provider == "google_drive":
            return _clean(payload.get("name"))
        if provider == "zendesk":
            return _clean(payload.get("subject"))
        if provider == "gmail":
            return _clean(_gmail_subject(payload))
    except Exception:  # noqa: BLE001 — a title is nice-to-have; never fail the caller
        log.warning("source_document: %s title extraction failed", provider, exc_info=True)
    return None  # slack: a message has no title


def _github_title(payload: dict) -> str | None:
    """PR/issue title — from the item itself or its webhook envelope."""
    for holder in (payload, payload.get("issue"), payload.get("pull_request")):
        if isinstance(holder, dict) and holder.get("title"):
            return holder["title"]
    return None


def _gmail_subject(payload: dict) -> str | None:
    headers = ((payload.get("payload") or {}).get("headers")) or []
    for h in headers:
        if (h.get("name") or "").lower() == "subject":
            return h.get("value")
    return None


def _clean(value: str | None) -> str | None:
    text = (value or "").strip()
    return text[:200] or None
