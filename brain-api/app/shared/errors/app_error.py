"""Typed application error hierarchy (BACKEND_BEST_PRACTICES.md §10).

Routers and services ``raise`` these; they never format HTTP errors inline. A
single set of exception handlers (shared/middleware/error_handler.py) maps them
to the standard error envelope. Each error carries an HTTP ``status`` and a
stable machine ``code``.
"""
from __future__ import annotations

from typing import Any


class AppError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


class ValidationError(AppError):
    def __init__(self, details: Any = None) -> None:
        super().__init__(422, "validation_error", "Request is invalid.", details)


class UnauthorizedError(AppError):
    def __init__(self, message: str = "Authentication required.") -> None:
        super().__init__(401, "unauthorized", message)


class ForbiddenError(AppError):
    def __init__(self, message: str = "You are not allowed to do that.") -> None:
        super().__init__(403, "forbidden", message)


class NotFoundError(AppError):
    def __init__(self, resource: str = "Resource") -> None:
        super().__init__(404, "not_found", f"{resource} not found.")


class ConflictError(AppError):
    def __init__(self, message: str = "Conflict") -> None:
        super().__init__(409, "conflict", message)


class RateLimitError(AppError):
    def __init__(self, retry_after: int) -> None:
        super().__init__(
            429,
            "rate_limited",
            "Too many requests. Try again later.",
            {"retryAfterSeconds": retry_after},
        )
