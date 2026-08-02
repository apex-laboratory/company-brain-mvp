"""Test configuration for the auth module.

Seeds valid env vars before any ``app.config.settings`` import so Pydantic
Settings validates cleanly. These are dummy values; tests that need a real DB or
Redis are integration tests and skip when the stack is unavailable.

Also severs the job queue from the real Redis for the whole suite — see
``_never_touch_the_real_queue``.
"""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_DEFAULTS = {
    "ENVIRONMENT": "test",
    "DATABASE_URL": "postgresql+asyncpg://test:test@localhost/test",
    "REDIS_URL": "redis://localhost:6379",
    "JWT_ACCESS_SECRET": "test-access-secret-at-least-32-characters-long",
    "JWT_REFRESH_SECRET": "test-refresh-secret-at-least-32-characters-long",
    "ENCRYPTION_KEY": "0" * 64,
    "ALLOWED_ORIGINS": "http://localhost:3000",
    "AI_SERVICE_URL": "http://localhost:8000",
    "AI_SERVICE_TOKEN": "test-ai-token",
    "RESEND_API_KEY": "test-resend-key",
}

for _key, _value in _DEFAULTS.items():
    os.environ.setdefault(_key, _value)


@pytest.fixture(autouse=True)
def _never_touch_the_real_queue():
    """Stop unit tests from enqueuing jobs onto the developer's live Redis.

    ``REDIS_URL`` above is only a *default*, and it is the same address a local
    worker consumes — so any code path that reaches ``app.jobs.queue.enqueue``
    without the test stubbing it writes a real job onto the real queue. The
    worker then picks it up with mock arguments and dies on them, e.g. a
    ``webhook_ingest`` fan-out test whose ``insert_event`` returns ``True``
    enqueues ``extract_event(workspace_id, True)`` and the worker fails with
    ``cannot cast type boolean to uuid``.

    Nothing surfaced this because ``enqueue`` swallows every exception by design
    (a queue outage must not fail a user request), so the leak was silent in both
    directions: the test passed and the failure landed in a different process.

    Patching ``get_queue`` rather than ``enqueue`` keeps the real ``enqueue``
    body — and its error handling — under test, while the pool it reaches is a
    mock. Tests that patch ``enqueue`` at their own module's import site are
    unaffected.
    """
    pool = MagicMock(enqueue_job=AsyncMock(return_value=None))
    with patch("app.jobs.queue.get_queue", AsyncMock(return_value=pool)):
        yield pool
