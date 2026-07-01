"""Webhooks router (BACKEND_BEST_PRACTICES.md §2, §6).

Mounted under ``/api/v1`` by ``main.py``. No auth dependency — providers can't
carry our JWT; trust comes from per-provider signature verification in the
service. The raw request body is read **before** any parsing so the HMAC is
computed over the exact bytes the provider signed.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.modules.webhooks.service import WebhooksService
from app.shared.http.respond import ok

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

_service = WebhooksService()


@router.post("/{provider}")
async def receive(provider: str, request: Request):
    """Receive, verify, and enqueue a provider webhook; respond 200 immediately.

    Slack's ``url_verification`` handshake returns a challenge that must be echoed
    verbatim so the endpoint can be registered in the Slack app console.
    """
    raw_body = await request.body()
    challenge = await _service.receive(provider, request.headers, raw_body)
    if challenge is not None:
        return JSONResponse({"challenge": challenge})
    return ok(request, {"status": "accepted"})
