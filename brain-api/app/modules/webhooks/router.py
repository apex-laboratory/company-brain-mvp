"""Webhooks router (BACKEND_BEST_PRACTICES.md §2, §6).

Mounted under ``/api/v1`` by ``main.py``. No auth dependency — providers can't
carry our JWT; trust comes from per-provider signature verification in the
service. The raw request body is read **before** any parsing so the HMAC is
computed over the exact bytes the provider signed.
"""
from __future__ import annotations

from fastapi import APIRouter, Request

from app.modules.webhooks.service import WebhooksService
from app.shared.http.respond import ok

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

_service = WebhooksService()


@router.post("/{provider}")
async def receive(provider: str, request: Request):
    """Receive, verify, and enqueue a provider webhook; respond 200 immediately."""
    raw_body = await request.body()
    await _service.receive(provider, request.headers, raw_body)
    return ok(request, {"status": "accepted"})
