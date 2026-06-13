from __future__ import annotations

import pytest
from starlette.requests import Request

from app.shared.errors.app_error import RateLimitError
from app.shared.middleware import rate_limit
from app.shared.middleware.rate_limit import enforce_api_key_rate_limit


class _FakeRedis:
    """Minimal in-memory stand-in for the counters used by the limiter."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.ttls: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    async def expire(self, key: str, seconds: int) -> None:
        self.ttls[key] = seconds

    async def ttl(self, key: str) -> int:
        return self.ttls.get(key, -1)


def _request(client_ip: str = "203.0.113.7") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [],
            "client": (client_ip, 12345),
            "state": {},
        }
    )


@pytest.mark.asyncio
async def test_allows_up_to_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeRedis()
    monkeypatch.setattr(rate_limit, "get_redis", lambda: fake)
    request = _request()
    # 10/minute — the first ten probes pass.
    for _ in range(10):
        await enforce_api_key_rate_limit(request)
    # The window TTL is set exactly once, on the first request.
    assert fake.ttls["ratelimit:apikey:203.0.113.7"] == 60


@pytest.mark.asyncio
async def test_blocks_the_eleventh_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeRedis()
    monkeypatch.setattr(rate_limit, "get_redis", lambda: fake)
    request = _request()
    for _ in range(10):
        await enforce_api_key_rate_limit(request)
    with pytest.raises(RateLimitError) as exc:
        await enforce_api_key_rate_limit(request)
    assert exc.value.status == 429
    assert exc.value.details == {"retryAfterSeconds": 60}


@pytest.mark.asyncio
async def test_limit_is_per_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeRedis()
    monkeypatch.setattr(rate_limit, "get_redis", lambda: fake)
    for _ in range(10):
        await enforce_api_key_rate_limit(_request("198.51.100.1"))
    # A different IP starts from a fresh window.
    await enforce_api_key_rate_limit(_request("198.51.100.2"))
