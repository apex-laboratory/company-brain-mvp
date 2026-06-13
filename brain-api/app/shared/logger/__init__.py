"""structlog JSON logging with field redaction (BACKEND_BEST_PRACTICES.md §11).

One bound logger per request carries request_id/user_id/workspace_id (bound in
the request-context middleware). A redaction processor scrubs any field whose
name looks like a secret (tokens, API keys, passwords, cookies, raw emails) so
secrets never reach the logs.
"""
from __future__ import annotations

import logging
from typing import cast

import structlog
from structlog.types import EventDict, WrappedLogger

from app.config.settings import settings

# Substrings that mark a field as sensitive. Matched case-insensitively against
# every key in the event dict.
REDACTED_KEYS: frozenset[str] = frozenset(
    {
        "authorization",
        "cookie",
        "token",
        "access_token",
        "refresh_token",
        "api_key",
        "apikey",
        "password",
        "secret",
        "encryption_key",
        "email",
        "oauth_code",  # oauth authorization codes (name the field oauth_code)
    }
)

_REDACTED = "[REDACTED]"


def _redact_sensitive(
    logger: WrappedLogger,
    method_name: str,
    event_dict: EventDict,
) -> EventDict:
    for key in list(event_dict.keys()):
        lowered = key.lower()
        if any(marker in lowered for marker in REDACTED_KEYS):
            event_dict[key] = _REDACTED
    return event_dict


structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        _redact_sensitive,
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(
        logging.DEBUG if settings.environment != "production" else logging.INFO
    ),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=True,
)


def get_logger() -> structlog.stdlib.BoundLogger:
    """Return the process-wide structlog logger."""
    return cast(structlog.stdlib.BoundLogger, structlog.get_logger())
