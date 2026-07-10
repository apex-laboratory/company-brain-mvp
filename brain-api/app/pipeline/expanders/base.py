"""Context-expander protocol, request bundle, and provider registry.

An expander receives an :class:`ExpandRequest` (a valid access token, the
connection's ``external_account_id`` for providers that need account context —
Jira cloudId, Google email — the raw event payload, and the already-normalized
content) and returns an :class:`ExpandedContext` (``app.pipeline.types``).

Providers without a registered expander (Gmail — the fetched message is already
the full body) are handled by the caller gating on ``needs_expansion`` before ever
calling ``get_expander``; that membership check is the single "no expander"
mechanism.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.pipeline.types import ExpandedContext


@dataclass(frozen=True)
class ExpandRequest:
    provider: str
    token: str
    account_id: str | None  # connection.external_account_id (cloudId, subdomain, email…)
    payload: dict  # source_events.payload (the provider-shaped raw item)
    content: str  # the already-normalized item content (fallback text)


@runtime_checkable
class ContextExpander(Protocol):
    async def expand(self, req: ExpandRequest) -> ExpandedContext: ...


def _build_registry() -> dict[str, ContextExpander]:
    # Imported lazily so a provider module's import error can't break the whole
    # pipeline package at import time.
    from app.pipeline.expanders.github import GitHubExpander
    from app.pipeline.expanders.google_drive import GoogleDriveExpander
    from app.pipeline.expanders.jira import JiraExpander
    from app.pipeline.expanders.notion import NotionExpander
    from app.pipeline.expanders.slack import SlackExpander
    from app.pipeline.expanders.zendesk import ZendeskExpander

    return {
        "slack": SlackExpander(),
        "notion": NotionExpander(),
        "github": GitHubExpander(),
        "zendesk": ZendeskExpander(),
        "jira": JiraExpander(),
        "google_drive": GoogleDriveExpander(),
        # gmail → passthrough (the fetched message already carries the full body)
    }


_REGISTRY: dict[str, ContextExpander] | None = None


def _registry() -> dict[str, ContextExpander]:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = _build_registry()
    return _REGISTRY


def needs_expansion(provider: str) -> bool:
    """True if ``provider`` has a real expander (not the passthrough)."""
    return provider in _registry()


def get_expander(provider: str) -> ContextExpander:
    """Return the registered expander for ``provider``.

    Callers must gate on ``needs_expansion(provider)`` first (the orchestrator
    does); a provider without an expander is a programming error here, not a
    silent passthrough."""
    return _registry()[provider]
