"""Centralized exception handlers (BACKEND_BEST_PRACTICES.md §10).

Registered once at app startup. Maps typed ``AppError``s and FastAPI validation
errors to the standard error envelope, and catches anything unhandled as a 500 —
never leaking stack traces, SQL, or provider payloads to the client.
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.shared.errors.app_error import AppError
from app.shared.http.respond import error_response
from app.shared.logger import get_logger

log = get_logger()


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def pydantic_validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        details = [
            {"path": ".".join(str(p) for p in e["loc"]), "message": e["msg"]}
            for e in exc.errors()
        ]
        return error_response(
            request,
            status=422,
            code="validation_error",
            message="Request is invalid.",
            details=details,
        )

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        if exc.status >= 500:
            log.error("request_failed", error_code=exc.code)
        else:
            log.warning("request_rejected", error_code=exc.code)
        headers: dict[str, str] = {}
        if exc.code == "rate_limited" and isinstance(exc.details, dict):
            retry = exc.details.get("retryAfterSeconds")
            if isinstance(retry, int):
                headers["Retry-After"] = str(retry)
        return error_response(
            request,
            status=exc.status,
            code=exc.code,
            message=exc.message,
            details=exc.details,
            headers=headers,
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled_error")
        return error_response(
            request,
            status=500,
            code="internal_error",
            message="Internal Server Error",
        )
