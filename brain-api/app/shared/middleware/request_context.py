"""Per-request context (BACKEND_BEST_PRACTICES.md §6, §11).

Assigns a ``req_`` request id, stores it on ``request.state``, binds it (plus
auth context, when resolved) to structlog's contextvars so every log line in the
request carries it, and echoes it back as the ``X-Request-ID`` response header.
"""
from __future__ import annotations

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.shared.helpers.ids import generate_id


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        request_id = generate_id("request")
        request.state.request_id = request_id

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        try:
            response = await call_next(request)
        finally:
            structlog.contextvars.clear_contextvars()

        response.headers["X-Request-ID"] = request_id
        return response
