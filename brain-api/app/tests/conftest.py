"""Test configuration for the auth module.

Seeds valid env vars before any ``app.config.settings`` import so Pydantic
Settings validates cleanly. These are dummy values; tests that need a real DB or
Redis are integration tests and skip when the stack is unavailable.
"""
from __future__ import annotations

import os

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
