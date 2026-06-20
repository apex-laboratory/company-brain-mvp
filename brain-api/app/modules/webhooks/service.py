"""Webhook receiver logic (BACKEND_BEST_PRACTICES.md §2).

Providers POST here with no dashboard credential, so trust is established by
**signature verification over the raw body** (HMAC, per provider) rather than our
auth middleware. Verified events are handed to the ARQ ``webhook_ingest`` job and
the request returns ``200`` immediately — no inline processing.

Signing-secret resolution is provider-specific: app-level secrets (e.g. Slack)
come from settings; per-subscription secrets (e.g. GitHub) come from
``webhook_subscriptions.secret_enc``. Each webhook-capable connector wires its own
secret source as it lands; Notion has no webhooks (``verify_webhook`` → ``False``).
"""
from __future__ import annotations

import json
from collections.abc import Mapping

from app.config.settings import settings
from app.integrations import get_integration
from app.jobs.queue import enqueue
from app.shared.errors.app_error import NotFoundError, UnauthorizedError, ValidationError


def _secret_for(provider: str) -> str:
    """Resolve the signing secret for a provider's webhook verification.

    App-level secrets come from settings (GitHub's shared App webhook secret).
    Providers whose ``verify_webhook`` ignores the secret (e.g. Notion, which has no
    webhooks) get ``""``. Per-subscription secrets (``webhook_subscriptions.secret_enc``)
    can be threaded here when a provider needs them.
    """
    return {"github": settings.github_webhook_secret}.get(provider, "")


class WebhooksService:
    async def receive(self, provider: str, headers: Mapping[str, str], raw_body: bytes) -> None:
        try:
            integration = get_integration(provider)
        except KeyError:
            raise NotFoundError("Webhook provider")

        if not integration.verify_webhook(headers, raw_body, _secret_for(provider)):
            raise UnauthorizedError("Webhook signature verification failed.")

        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError:
            raise ValidationError({"body": "Webhook body is not valid JSON."})

        # Enqueue and return fast; the job dedupes + normalizes + persists.
        await enqueue("webhook_ingest", provider, payload)
