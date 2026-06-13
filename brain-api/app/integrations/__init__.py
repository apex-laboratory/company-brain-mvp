"""Provider integrations (BACKEND_BEST_PRACTICES.md §2).

Each module isolates one external provider's OAuth + read-only fetch behind the
``SourceIntegration`` protocol. If a vendor changes, only its file changes; the
sources/webhooks modules and the sync jobs depend only on the protocol.
"""
from __future__ import annotations

from app.integrations.base import (
    ChannelRef,
    OAuthTokens,
    RawEvent,
    RawItem,
    SourceIntegration,
)
from app.integrations.notion import NotionIntegration

# Provider → integration instance. The sources/webhooks layers and the sync jobs
# resolve providers through this registry only.
REGISTRY: dict[str, SourceIntegration] = {
    "notion": NotionIntegration(),
}


def get_integration(provider: str) -> SourceIntegration:
    """Return the integration for ``provider`` or raise ``KeyError``."""
    return REGISTRY[provider]


__all__ = [
    "ChannelRef",
    "OAuthTokens",
    "RawEvent",
    "RawItem",
    "SourceIntegration",
    "NotionIntegration",
    "REGISTRY",
    "get_integration",
]
