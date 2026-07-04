"""Provider integrations (BACKEND_BEST_PRACTICES.md §2).

Each module isolates one external provider's OAuth + read-only fetch behind the
``SourceIntegration`` protocol. If a vendor changes, only its file changes; the
sources/webhooks modules and the sync jobs depend only on the protocol.
"""
from __future__ import annotations

from app.integrations.base import (
    ChannelRef,
    ConnectorAuthError,
    OAuthTokens,
    RawEvent,
    RawItem,
    SourceIntegration,
)
from app.integrations.github import GitHubIntegration
from app.integrations.gmail import GmailIntegration
from app.integrations.google_drive import GoogleDriveIntegration
from app.integrations.jira import JiraIntegration
from app.integrations.notion import NotionIntegration
from app.integrations.slack import SlackIntegration
from app.integrations.zendesk import ZendeskIntegration

# Provider → integration instance. The sources/webhooks layers and the sync jobs
# resolve providers through this registry only.
REGISTRY: dict[str, SourceIntegration] = {
    "notion": NotionIntegration(),
    "github": GitHubIntegration(),
    "slack": SlackIntegration(),
    "google_drive": GoogleDriveIntegration(),
    "gmail": GmailIntegration(),
    "zendesk": ZendeskIntegration(),
    "jira": JiraIntegration(),
}


def get_integration(provider: str) -> SourceIntegration:
    """Return the integration for ``provider`` or raise ``KeyError``."""
    return REGISTRY[provider]


__all__ = [
    "ChannelRef",
    "ConnectorAuthError",
    "OAuthTokens",
    "RawEvent",
    "RawItem",
    "SourceIntegration",
    "NotionIntegration",
    "GitHubIntegration",
    "SlackIntegration",
    "GoogleDriveIntegration",
    "GmailIntegration",
    "ZendeskIntegration",
    "JiraIntegration",
    "REGISTRY",
    "get_integration",
]
