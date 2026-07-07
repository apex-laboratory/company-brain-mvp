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

import base64
import json
from collections.abc import Mapping

from app.config.database import get_session
from app.config.settings import settings
from app.integrations import get_integration
from app.integrations.google_common import GOOGLE_PUSH_PROVIDERS
from app.jobs.queue import enqueue
from app.jobs.repository import JobsRepository
from app.shared.errors.app_error import NotFoundError, UnauthorizedError, ValidationError
from app.shared.helpers.crypto import constant_time_compare, decrypt

_repo = JobsRepository()


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive header lookup (Starlette Headers are already insensitive;
    a plain ``dict`` in tests is not)."""
    if name in headers:
        return headers[name]
    lower = name.lower()
    for key, value in headers.items():
        if key.lower() == lower:
            return value
    return None


def _secret_for(provider: str) -> str:
    """Resolve the signing secret for a provider's webhook verification.

    App-level secrets come from settings (GitHub's shared App webhook secret).
    Providers whose ``verify_webhook`` ignores the secret (e.g. Notion, which has no
    webhooks) get ``""``. Per-subscription secrets (``webhook_subscriptions.secret_enc``)
    can be threaded here when a provider needs them.
    """
    return {
        "github": settings.github_webhook_secret,
        "slack": settings.slack_signing_secret,
    }.get(provider, "")


class WebhooksService:
    async def receive(
        self,
        provider: str,
        headers: Mapping[str, str],
        raw_body: bytes,
        query_params: Mapping[str, str] | None = None,
    ) -> str | dict | None:
        """Verify + enqueue a provider webhook.

        Returns a **challenge string** to echo verbatim (Slack's Events API
        ``url_verification`` handshake, which must be answered synchronously so the
        endpoint can be registered), a dict routing result (Google push), or ``None``
        for a normal event that was enqueued.
        """
        try:
            integration = get_integration(provider)
        except KeyError:
            raise NotFoundError("Webhook provider")

        # Google push (Drive/Gmail) is content-free: verify + route here (we have the
        # headers/query params), then trigger the incremental sweep. It never goes
        # through webhook_ingest/normalize.
        if provider in GOOGLE_PUSH_PROVIDERS:
            return await self._receive_google(provider, headers, raw_body, query_params or {})

        if not integration.verify_webhook(headers, raw_body, _secret_for(provider)):
            raise UnauthorizedError("Webhook signature verification failed.")

        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError:
            raise ValidationError({"body": "Webhook body is not valid JSON."})

        # Slack Events API URL handshake: the (signature-verified) url_verification
        # request must be answered by echoing its challenge; it is not an ingestible
        # event, so return the challenge here instead of enqueuing.
        if isinstance(payload, dict) and payload.get("type") == "url_verification":
            return payload.get("challenge")

        # Enqueue and return fast; the job dedupes + normalizes + persists.
        await enqueue("webhook_ingest", provider, payload)
        return None

    async def _receive_google(
        self,
        provider: str,
        headers: Mapping[str, str],
        raw_body: bytes,
        query_params: Mapping[str, str],
    ) -> dict | None:
        if provider == "google_drive":
            channel_id = _header(headers, "X-Goog-Channel-ID")
            if not channel_id:
                raise UnauthorizedError("Missing Drive channel id.")
            async with get_session() as session:
                sub = await _repo.get_subscription_by_ref(session, provider, channel_id)
            if sub is None:
                raise UnauthorizedError("Unknown Drive channel.")
            workspace_id, source_id, secret_enc = sub
            secret = decrypt(secret_enc.decode()) if secret_enc else ""
            integration = get_integration(provider)
            if not integration.verify_webhook(headers, raw_body, secret):
                raise UnauthorizedError("Drive channel token verification failed.")
            # The initial handshake ping carries no change — just acknowledge it.
            if _header(headers, "X-Goog-Resource-State") == "sync":
                return None
            await enqueue("source_sync", workspace_id, source_id)
            return None

        # provider == "gmail": Cloud Pub/Sub push, verified by the URL token.
        token = query_params.get("token", "")
        expected = settings.google_pubsub_verification_token
        if not expected or not constant_time_compare(token.encode(), expected.encode()):
            raise UnauthorizedError("Gmail push token verification failed.")
        email = _decode_pubsub_email(raw_body)
        if email is None:
            raise ValidationError({"body": "Gmail push body missing emailAddress."})
        async with get_session() as session:
            connections = await _repo.resolve_all_by_account(session, provider, email)
        if not connections:
            # Delivery for a mailbox we no longer track — acknowledge and drop.
            return None
        # The same mailbox can be connected in multiple workspaces; sync each one.
        for source_id, workspace_id in connections:
            await enqueue("source_sync", workspace_id, source_id)
        return None


def _decode_pubsub_email(raw_body: bytes) -> str | None:
    """Pull ``emailAddress`` out of a Gmail Cloud Pub/Sub push envelope."""
    try:
        envelope = json.loads(raw_body)
        data = envelope["message"]["data"]
        decoded = json.loads(base64.b64decode(data).decode("utf-8"))
        return decoded.get("emailAddress")
    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
        return None
