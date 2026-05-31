from .base import SourceConnector


class GitHubConnector(SourceConnector):
    def __init__(self, access_token: str) -> None:
        self._token = access_token

    async def fetch_context(self, event_payload: dict) -> dict:
        raise NotImplementedError("Phase 2")

    async def fetch_historical(self, target_id: str, lookback_days: int) -> list[dict]:
        raise NotImplementedError("Phase 2")

    async def search(self, query: str, monitored_ids: list[str]) -> list[dict]:
        raise NotImplementedError("Phase 2")

    async def subscribe_webhook(self, target_id: str, callback_url: str) -> str:
        raise NotImplementedError("Phase 2")

    async def unsubscribe_webhook(self, subscription_id: str) -> None:
        raise NotImplementedError("Phase 2")
