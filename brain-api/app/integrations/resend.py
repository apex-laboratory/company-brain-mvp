"""Resend transactional email integration (BACKEND_BEST_PRACTICES.md §1, §12).

The single place the Resend HTTP API and the ``RESEND_API_KEY`` credential are
used — every email (member invites, auth, alerts) goes through :func:`send_email`
so the vendor is isolated behind one module. All calls use a 10-second timeout so
a hung Resend never stalls the request.

The API key and default sender are read from validated settings, so once
``RESEND_API_KEY`` is set in the environment this module works with no further
wiring. ``send_email`` raises ``EmailError`` on any transport or API failure;
callers decide whether that should fail their request or be best-effort.
"""
from __future__ import annotations

import httpx

from app.config.settings import settings
from app.shared.logger import get_logger

log = get_logger()

_SEND_URL = "https://api.resend.com/emails"
_TIMEOUT = httpx.Timeout(10.0)


class EmailError(Exception):
    """Resend rejected the send or the request failed in transit."""


async def send_email(
    *,
    to: str,
    subject: str,
    html: str,
    text: str | None = None,
    from_email: str | None = None,
) -> str:
    """Send one email via Resend; return the provider message id.

    ``from_email`` defaults to ``settings.default_from_email``. Raises
    ``EmailError`` on a network failure or a non-2xx response — the raw key and
    recipient are never included in the raised message or logs.
    """
    payload: dict[str, object] = {
        "from": from_email or settings.default_from_email,
        "to": [to],
        "subject": subject,
        "html": html,
    }
    if text is not None:
        payload["text"] = text

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                _SEND_URL,
                json=payload,
                headers={"Authorization": f"Bearer {settings.resend_api_key}"},
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as exc:
        # Surface a clean failure; the structlog redactor keeps any token field
        # out of logs, but we deliberately log no credential here regardless.
        log.warning("resend_send_failed", error=str(exc))
        raise EmailError("Email delivery failed.") from exc

    message_id = data.get("id") if isinstance(data, dict) else None
    if not message_id:
        log.warning("resend_send_no_id")
        raise EmailError("Email provider returned no message id.")
    return str(message_id)
