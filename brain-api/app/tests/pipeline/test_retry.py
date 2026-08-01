"""Unit tests for app/pipeline/llm/retry.py — transient classification + backoff."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.pipeline.llm.retry import LLMExhaustedError, is_transient, with_retries


class _FakeSDKError(Exception):
    """Stands in for a provider SDK error; module origin drives is_transient."""

    def __init__(self, message: str = "", status_code: int | None = None, headers=None):
        super().__init__(message)
        if status_code is not None:
            self.status_code = status_code
        if headers is not None:
            self.response = type("R", (), {"headers": headers})()


# Pretend the fake error comes from the Gemini SDK so connection-style errors
# (no status_code) classify as transient.
_FakeSDKError.__module__ = "google.genai"


# ── classification ───────────────────────────────────────────────────────────

def test_429_is_transient() -> None:
    assert is_transient(_FakeSDKError(status_code=429))


def test_5xx_is_transient() -> None:
    assert is_transient(_FakeSDKError(status_code=503))


def test_connection_error_is_transient() -> None:
    assert is_transient(_FakeSDKError("connection reset"))


def test_400_is_permanent() -> None:
    assert not is_transient(_FakeSDKError(status_code=400))


def test_401_is_permanent() -> None:
    assert not is_transient(_FakeSDKError(status_code=401))


def test_programming_error_is_permanent() -> None:
    assert not is_transient(TypeError("oops"))


# ── retry behavior ───────────────────────────────────────────────────────────

async def test_success_first_try_no_sleep() -> None:
    call = AsyncMock(return_value="ok")
    with patch("app.pipeline.llm.retry.asyncio.sleep") as sleep:
        assert await with_retries(call, stage="t", attempts=3) == "ok"
    call.assert_awaited_once()
    sleep.assert_not_called()


async def test_transient_then_success_backs_off_exponentially() -> None:
    errors = [_FakeSDKError(status_code=429), _FakeSDKError(status_code=500)]
    call = AsyncMock(side_effect=[*errors, "ok"])
    with patch("app.pipeline.llm.retry.asyncio.sleep") as sleep:
        assert await with_retries(call, stage="t", attempts=3) == "ok"
    assert call.await_count == 3
    assert [c.args[0] for c in sleep.await_args_list] == [1.0, 2.0]  # 2**0, 2**1


async def test_retry_after_header_honored_and_capped() -> None:
    err = _FakeSDKError(status_code=429, headers={"retry-after": "500"})
    call = AsyncMock(side_effect=[err, "ok"])
    with patch("app.pipeline.llm.retry.asyncio.sleep") as sleep:
        assert await with_retries(call, stage="t", attempts=3) == "ok"
    assert sleep.await_args_list[0].args[0] == 400.0  # capped at _MAX_RETRY_AFTER


async def test_permanent_error_raises_immediately() -> None:
    call = AsyncMock(side_effect=_FakeSDKError(status_code=400))
    with patch("app.pipeline.llm.retry.asyncio.sleep") as sleep, pytest.raises(_FakeSDKError):
        await with_retries(call, stage="t", attempts=3)
    call.assert_awaited_once()
    sleep.assert_not_called()


async def test_exhausted_retries_raise_llm_exhausted_chained() -> None:
    call = AsyncMock(side_effect=_FakeSDKError(status_code=429))
    with patch("app.pipeline.llm.retry.asyncio.sleep"), pytest.raises(LLMExhaustedError) as excinfo:
        await with_retries(call, stage="gate", attempts=3)
    assert call.await_count == 3
    assert isinstance(excinfo.value.__cause__, _FakeSDKError)
    assert "gate" in str(excinfo.value)
