"""Per-source context expanders (Phase 3 — KAN-10–14).

A raw ingested event carries only the item that triggered it (one Slack message,
one Jira issue, …). After the relevance gate decides an item is worth extracting,
the matching expander pulls the surrounding context — the full thread, the ticket
comments, the page body — so the extractor sees the whole conversation, not a
single line.

Expanders are best-effort: the orchestrator falls back to the raw content if one
fails, so a provider outage degrades extraction quality but never blocks it.
"""
from app.pipeline.expanders.base import (
    ExpandRequest,
    get_expander,
    needs_expansion,
)

__all__ = ["ExpandRequest", "get_expander", "needs_expansion"]
