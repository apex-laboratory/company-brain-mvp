"""Brain chat router (BACKEND_ASKS §7, BRAIN_CHAT_RAG_PLAN).

Mounted under ``/api/v1`` as ``/brain``. Reads are gated by ``require_brain_access``
(API keys need the ``brain:query`` scope; dashboard JWTs need role ≥ viewer), so the
one surface serves both the browser dashboard and agents. Everything is RLS-scoped
in the service; these gates are defense in depth.
"""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse

from app.modules.brain.schemas import BrainQueryRequest
from app.modules.brain.service import BrainService
from app.shared.errors.app_error import AppError
from app.shared.http.respond import ok
from app.shared.middleware.authenticate import AuthContext, get_auth_context
from app.shared.middleware.authorize import require_brain_access
from app.shared.middleware.rate_limit import BRAIN_LIMIT, limiter, workspace_key

router = APIRouter(prefix="/brain", tags=["brain"])
log = logging.getLogger(__name__)

_service = BrainService()


async def _sse(events: AsyncIterator[tuple[str, dict]]) -> AsyncIterator[str]:
    """Serialize ``(event, payload)`` pairs as SSE frames.

    The 200 status line is committed the moment the first frame flushes, so a failure
    part-way through can't become an HTTP error — it is reported as a terminal
    ``error`` event carrying the same machine ``code`` the JSON envelope would have
    used, and the client surfaces that instead of a half-written answer.
    """
    try:
        async for event, payload in events:
            yield f"event: {event}\ndata: {json.dumps(jsonable_encoder(payload))}\n\n"
    except AppError as exc:
        payload = {"code": exc.code, "message": exc.message}
        yield f"event: error\ndata: {json.dumps(payload)}\n\n"
    except Exception:  # noqa: BLE001 — the stream is already open; report, don't 500
        log.exception("brain: query stream failed")
        payload = {"code": "internal_error", "message": "The answer stream failed."}
        yield f"event: error\ndata: {json.dumps(payload)}\n\n"


@router.get("/status", dependencies=[Depends(require_brain_access("brain:query"))])
@limiter.limit(BRAIN_LIMIT, key_func=workspace_key)
async def brain_status(
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    """Whether the brain is ready to answer for this workspace (FE gate)."""
    status = await _service.status(auth)
    return ok(request, status.model_dump(by_alias=True))


@router.post("/query", dependencies=[Depends(require_brain_access("brain:query"))])
@limiter.limit(BRAIN_LIMIT, key_func=workspace_key)
async def brain_query(
    request: Request,
    body: BrainQueryRequest,
    auth: AuthContext = Depends(get_auth_context),
):
    """Answer an operational question with a cited, confidence-scored answer.

    One surface for both the browser dashboard (JWT) and agents (API-key); a
    below-threshold question returns an honest no-match, and an unready workspace
    a typed 409 (``brain_not_ready``) before any embedding/LLM spend.
    """
    answer = await _service.query(auth, body.question, body.conversation_id)
    return ok(request, answer.model_dump(by_alias=True))


@router.post(
    "/query/stream", dependencies=[Depends(require_brain_access("brain:query"))]
)
@limiter.limit(BRAIN_LIMIT, key_func=workspace_key)
async def brain_query_stream(
    request: Request,
    body: BrainQueryRequest,
    auth: AuthContext = Depends(get_auth_context),
):
    """Server-sent-events twin of ``POST /brain/query`` — same answer, incremental.

    Event sequence: ``status`` (``retrieving`` → ``synthesizing``) → ``token``* →
    ``done`` (the full ``BrainQueryResponse``, identical to the non-streaming route).
    ``token`` events appear only once the answer is known to be grounded, so a
    no-match streams no text — just ``done`` with the honest miss. Readiness is
    checked before the stream opens, so an unready workspace still 409s.
    """
    events = await _service.start_stream(auth, body.question, body.conversation_id)
    return StreamingResponse(
        _sse(events),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get(
    "/conversations", dependencies=[Depends(require_brain_access("brain:query"))]
)
@limiter.limit(BRAIN_LIMIT, key_func=workspace_key)
async def list_conversations(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    auth: AuthContext = Depends(get_auth_context),
):
    """The signed-in user's chat threads, newest-active first (history sidebar).

    Dashboard (JWT) only — agents have no persisted conversations (403)."""
    conversations = await _service.list_conversations(auth, limit=limit)
    return ok(request, [c.model_dump(by_alias=True) for c in conversations])


@router.get(
    "/conversations/{conversation_id}/messages",
    dependencies=[Depends(require_brain_access("brain:query"))],
)
@limiter.limit(BRAIN_LIMIT, key_func=workspace_key)
async def list_conversation_messages(
    request: Request,
    conversation_id: str,
    auth: AuthContext = Depends(get_auth_context),
):
    """Replay one thread's turns (oldest first) so the FE can restore it on reload.

    404s an unknown/unowned conversation; dashboard (JWT) only."""
    messages = await _service.list_messages(auth, conversation_id)
    return ok(request, [m.model_dump(by_alias=True) for m in messages])
