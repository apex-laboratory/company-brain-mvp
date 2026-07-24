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


class ConfigurationError(AppError):
    """A server-side misconfiguration (e.g. a provider's OAuth credentials are unset).

    501 Not Implemented: the request is well-formed but the server isn't configured to
    fulfil it, so retrying with different input won't help — an operator must act.
    """

    def __init__(self, message: str = "This capability is not configured.") -> None:
        super().__init__(501, "not_configured", message)


class RateLimitError(AppError):
    def __init__(self, retry_after: int) -> None:
        super().__init__(
            429,
            "rate_limited",
            "Too many requests. Try again later.",
            {"retryAfterSeconds": retry_after},
        )


class BrainNotReadyError(AppError):
    """The "Ask the brain" chat surface is not available yet (BRAIN_CHAT_RAG_PLAN §0).

    409 with a machine ``reason`` so the dashboard renders the right disabled state
    — globally disabled, no skills indexed, or still indexing — instead of a silent
    failure or a fabricated answer. Raised by the readiness gate *before* any
    embedding or LLM spend.
    """

    _REASONS = ("disabled", "no_skills", "indexing")

    def __init__(self, reason: str = "no_skills", message: str | None = None) -> None:
        if reason not in self._REASONS:
            raise ValueError(f"unknown brain_not_ready reason: {reason!r}")
        super().__init__(
            409,
            "brain_not_ready",
            message or "The brain is not ready to answer questions yet.",
            {"reason": reason},
        )
