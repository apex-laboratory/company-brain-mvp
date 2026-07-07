"""Quota-aware retry tests for ``google_common.api_request`` (KAN-2).

Outbound HTTP is intercepted with ``httpx.MockTransport`` swapped into the shared
client — no network. ``asyncio.sleep`` inside the module is stubbed so backoff
takes no wall-clock time; the recorded delays assert the backoff schedule.
"""
from __future__ import annotations

import httpx
import pytest

from app.integrations import base
from app.integrations import google_common

_URL = "https://www.googleapis.com/drive/v3/files"


def _install_transport(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(base, "_client", client)


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    recorded: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(google_common.asyncio, "sleep", _fake_sleep)
    return recorded


def _quota_403_body() -> dict:
    return {
        "error": {
            "code": 403,
            "errors": [{"reason": "userRateLimitExceeded", "domain": "usageLimits"}],
        }
    }


async def test_429_retried_then_succeeds(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(200, json={"ok": True})

    _install_transport(monkeypatch, handler)
    resp = await google_common.api_request("GET", _URL)
    assert resp.json() == {"ok": True}
    assert calls["n"] == 2
    assert sleeps == [2.0]  # honored Retry-After, not exponential default


async def test_quota_403_exhausts_to_quota_error_not_status_error(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json=_quota_403_body())

    _install_transport(monkeypatch, handler)
    # Must NOT be httpx.HTTPStatusError: source_sync would classify a 403 as
    # broken auth and demand a re-auth for what is only a rate limit.
    with pytest.raises(google_common.QuotaExceededError):
        await google_common.api_request("GET", _URL)
    assert len(sleeps) == google_common._MAX_ATTEMPTS - 1


async def test_permission_403_raises_status_error_immediately(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403, json={"error": {"code": 403, "errors": [{"reason": "insufficientPermissions"}]}}
        )

    _install_transport(monkeypatch, handler)
    with pytest.raises(httpx.HTTPStatusError):
        await google_common.api_request("GET", _URL)
    assert sleeps == []  # genuine permission errors are not retried


async def test_resource_exhausted_status_counts_as_quota(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(403, json={"error": {"status": "RESOURCE_EXHAUSTED"}})
        return httpx.Response(200, json={"ok": True})

    _install_transport(monkeypatch, handler)
    resp = await google_common.api_request("GET", _URL)
    assert resp.status_code == 200
    assert calls["n"] == 2


async def test_5xx_retried_with_exponential_backoff(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= 2:
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": True})

    _install_transport(monkeypatch, handler)
    resp = await google_common.api_request("GET", _URL)
    assert resp.status_code == 200
    assert sleeps == [1.0, 2.0]  # 2**0, 2**1 — no Retry-After header


async def test_5xx_exhausted_raises_status_error(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    _install_transport(monkeypatch, handler)
    with pytest.raises(httpx.HTTPStatusError):
        await google_common.api_request("GET", _URL)
    assert len(sleeps) == google_common._MAX_ATTEMPTS - 1


async def test_401_passes_through_for_auth_classification(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    _install_transport(monkeypatch, handler)
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await google_common.api_request("GET", _URL)
    assert exc_info.value.response.status_code == 401
    assert sleeps == []
