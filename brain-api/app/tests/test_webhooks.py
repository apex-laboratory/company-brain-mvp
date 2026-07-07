"""Webhook receiver tests (KAN-2)."""
from __future__ import annotations

import pytest

from app.integrations import REGISTRY
from app.modules.webhooks import service as webhook_service
from app.modules.webhooks.service import WebhooksService
from app.shared.errors.app_error import NotFoundError, UnauthorizedError, ValidationError


class _AcceptingIntegration:
    provider = "fake"

    def verify_webhook(self, headers, raw_body, secret) -> bool:  # noqa: ANN001
        return True


async def test_unknown_provider_raises_not_found() -> None:
    with pytest.raises(NotFoundError):
        await WebhooksService().receive("dropbox", {}, b"{}")


async def test_notion_rejects_signature() -> None:
    # Notion has no webhooks, so verify_webhook is always False.
    with pytest.raises(UnauthorizedError):
        await WebhooksService().receive("notion", {}, b'{"any": "thing"}')


async def test_verified_event_is_enqueued(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(REGISTRY, "fake", _AcceptingIntegration())
    calls: list[tuple] = []

    async def fake_enqueue(function: str, *args, **kwargs) -> None:
        calls.append((function, args))

    monkeypatch.setattr(webhook_service, "enqueue", fake_enqueue)

    await WebhooksService().receive("fake", {}, b'{"team_id": "T1", "id": "E1"}')

    assert calls == [("webhook_ingest", ("fake", {"team_id": "T1", "id": "E1"}))]


async def test_invalid_json_after_verification_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(REGISTRY, "fake", _AcceptingIntegration())
    monkeypatch.setattr(webhook_service, "enqueue", lambda *a, **k: None)
    with pytest.raises(ValidationError):
        await WebhooksService().receive("fake", {}, b"not-json")
