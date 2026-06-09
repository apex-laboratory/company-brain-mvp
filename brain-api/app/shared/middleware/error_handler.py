"""Centralized exception handlers (BACKEND_BEST_PRACTICES.md §10).

Registered once at app startup. Maps typed ``AppError``s and FastAPI validation
errors to the standard error envelope, and catches anything unhandled as a 500 —
never leaking stack traces, SQL, or provider payloads to the client.
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.shared.errors.app_error import AppError
from app.shared.logger import get_logger

log = get_logger()


def _request_id(request: Request) -> str | None:
    rid = getattr(request.state, "request_id", None)
    return rid if isinstance(rid, str) else None


def _error_body(
    code: str, message: str, request_id: str | None, details: Any = None
) -> dict[str, Any]:
    return {
        "error": {"code": code, "message": message, "details": details},
        "meta": {"requestId": request_id},
    }


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def pydantic_validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        details = [
            {"path": ".".join(str(p) for p in e["loc"]), "message": e["msg"]}
            for e in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content=_error_body(
                "validation_error", "Request is invalid.", _request_id(request), details
            ),
        )

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        if exc.status >= 500:
            log.error("request_failed", code=exc.code)
        else:
            log.warning("request_rejected", code=exc.code)
        headers: dict[str, str] = {}
        if exc.code == "rate_limited" and isinstance(exc.details, dict):
            retry = exc.details.get("retryAfterSeconds")
            if isinstance(retry, int):
                headers["Retry-After"] = str(retry)
        return JSONResponse(
            status_code=exc.status,
            content=_error_body(exc.code, exc.message, _request_id(request), exc.details),
            headers=headers,
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled_error")
        return JSONResponse(
            status_code=500,
            content=_error_body("internal_error", "Internal Server Error", _request_id(request)),
        )
