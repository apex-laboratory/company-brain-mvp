from __future__ import annotations
from abc import ABC, abstractmethod


class SourceConnector(ABC):
    """
    Abstract base for all source API clients.

    Each source (Slack, Notion, GitHub, Jira, Zendesk) implements this interface.
    The connector handles OAuth token usage, context fetching, historical sweep,
    live search for query-driven extraction, and webhook lifecycle management.
    """

    @abstractmethod
    async def fetch_context(self, event_payload: dict) -> dict:
        """Fetch full surrounding context for a webhook event payload."""
        ...

    @abstractmethod
    async def fetch_historical(self, target_id: str, lookback_days: int) -> list[dict]:
        """Fetch historical items from a channel/space/project for the sweep."""
        ...

    @abstractmethod
    async def search(self, query: str, monitored_ids: list[str]) -> list[dict]:
        """Live search used by query-driven extraction fallback (Process 4)."""
        ...

    @abstractmethod
    async def subscribe_webhook(self, target_id: str, callback_url: str) -> str:
        """Create a webhook subscription. Returns the source-assigned subscription ID."""
        ...

    @abstractmethod
    async def unsubscribe_webhook(self, subscription_id: str) -> None:
        """Remove a webhook subscription."""
        ...
