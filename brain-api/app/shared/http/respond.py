"""Standard response envelope helpers (BACKEND_BEST_PRACTICES.md §14).

Every success response is ``{ "data": ..., "meta": { requestId, timestamp } }``.
Lists pass ``next_cursor`` (and any extra meta) through ``**meta``. Status codes
follow the API doc: 200 ok, 201 created, 202 accepted, 204 no content.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse


def _meta(request: Request, extra: dict[str, Any]) -> dict[str, Any]:
    return {
        "requestId": getattr(request.state, "request_id", None),
        "timestamp": datetime.now(UTC).isoformat(),
        **extra,
    }


def _envelope(request: Request, data: Any, status_code: int, meta: dict[str, Any]) -> JSONResponse:
    return JSONResponse(
        content=jsonable_encoder({"data": data, "meta": _meta(request, meta)}),
        status_code=status_code,
    )


def ok(request: Request, data: Any, **meta: Any) -> JSONResponse:
    """200 OK. Pass list pagination via ``nextCursor=...`` keyword meta."""
    return _envelope(request, data, 200, meta)


def created(request: Request, data: Any, **meta: Any) -> JSONResponse:
    """201 Created."""
    return _envelope(request, data, 201, meta)


def accepted(request: Request, data: Any, **meta: Any) -> JSONResponse:
    """202 Accepted (queued for background processing)."""
    return _envelope(request, data, 202, meta)


def no_content() -> Response:
    """204 No Content (empty body)."""
    return Response(status_code=204)
