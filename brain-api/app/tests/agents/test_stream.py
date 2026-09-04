"""SSE pass-through tests (agent-builder-plan §5.5, §4.6).

The rule this file exists to hold is "no accumulator", and an accumulator is
hard to assert the absence of directly — a proxy that buffered the whole turn and
then wrote it would produce byte-identical output. So it is pinned behaviourally
instead: the consumer must receive frame *n* before the upstream is asked for
frame *n+1*. A buffering proxy fails that and a streaming one cannot.

The other half is §4.6's metadata tap. What matters is not that it sees the right
names but that it *cannot* see anything else, so the assertion is about the bytes
that never reach it rather than the ones that do.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.modules.agents.stream import DELTA_EVENTS, open_session_stream
from app.shared.errors.app_error import NotFoundError

_SECRET = "the agent read /etc/passwd and here is what it said"

_FRAMES = [
    "event: agent.message",
    f'data: {{"type":"agent.message","content":[{{"text":"{_SECRET}"}}]}}',
    "",
    "event: session.status_idle",
    'data: {"type":"session.status_idle","stop_reason":{"type":"end_turn"}}',
    "",
]


class _Response:
    """The minimum of an ``httpx.Response`` this proxy touches."""

    def __init__(self, lines: list[str], pulls: list[str] | None = None) -> None:
        self._lines = lines
        self._pulls = pulls
        self.closed = False

    async def aiter_lines(self) -> AsyncIterator[str]:
        for line in self._lines:
            if self._pulls is not None:
                self._pulls.append(f"pull:{line[:14]}")
            yield line


def _client(
    lines: list[str] | None = None,
    pulls: list[str] | None = None,
    raises: Exception | None = None,
) -> tuple[MagicMock, _Response]:
    response = _Response(lines if lines is not None else _FRAMES, pulls)

    @asynccontextmanager
    async def _stream(*_: Any, **__: Any) -> AsyncIterator[Any]:
        if raises is not None:
            raise raises
        try:
            yield response
        finally:
            response.closed = True

    client = MagicMock()
    client.stream_events = _stream
    return client, response


async def _drain(frames: AsyncIterator[bytes]) -> list[str]:
    return [chunk.decode() async for chunk in frames]


@pytest.mark.asyncio
async def test_frames_are_forwarded_verbatim() -> None:
    """Every line goes out as it came in, with the SSE terminator restored.

    ``aiter_lines`` drops the terminator and yields "" for the blank line that
    ends an event, so re-adding one "\\n" rebuilds a well-formed stream.
    """
    client, _ = _client()
    frames = await open_session_stream(client, "ses_1")

    assert await _drain(frames) == [f"{line}\n" for line in _FRAMES]


@pytest.mark.asyncio
async def test_nothing_is_buffered_between_frames() -> None:
    """The absence-of-accumulator assertion. See the module docstring.

    Interleaving proves it: a proxy that collected the turn before writing would
    show every ``pull`` before every ``yield``.
    """
    events: list[str] = []
    client, _ = _client(lines=["a", "b", "c"], pulls=events)

    frames = await open_session_stream(client, "ses_1")
    async for chunk in frames:
        events.append(f"yield:{chunk.decode().strip()}")

    assert events == [
        "pull:a", "yield:a",
        "pull:b", "yield:b",
        "pull:c", "yield:c",
    ]


@pytest.mark.asyncio
async def test_the_tap_receives_event_names_and_cannot_receive_content() -> None:
    """§4.6's seam. Structural, not a promise: it is fed from ``event:`` lines only.

    The assertion that matters is the second one — the agent's actual output goes
    past the tap and is never offered to it, so an envelope builder wired here
    inherits an inability to read what the agent said.
    """
    seen: list[str] = []
    client, _ = _client()

    frames = await open_session_stream(client, "ses_1", on_event=seen.append)
    forwarded = "".join(await _drain(frames))

    assert seen == ["agent.message", "session.status_idle"]
    assert not any(_SECRET in observed for observed in seen)
    # Not vacuous: the content did flow through, it just never reached the tap.
    assert _SECRET in forwarded


@pytest.mark.asyncio
async def test_a_raising_tap_does_not_sever_the_stream() -> None:
    """The stream is the product; the tap is telemetry.

    Reversing this would let a bug in future envelope-building code look exactly
    like the agent crashing.
    """
    def _explode(_: str) -> None:
        raise RuntimeError("tap is broken")

    client, _ = _client()
    frames = await open_session_stream(client, "ses_1", on_event=_explode)

    assert await _drain(frames) == [f"{line}\n" for line in _FRAMES]


@pytest.mark.asyncio
async def test_the_upstream_closes_when_the_consumer_stops_early() -> None:
    """A browser that navigates away must not leave Anthropic writing into nothing."""
    client, response = _client()
    frames = await open_session_stream(client, "ses_1")

    await frames.__anext__()
    await frames.aclose()

    assert response.closed is True


@pytest.mark.asyncio
async def test_the_upstream_closes_after_a_complete_read() -> None:
    client, response = _client()
    frames = await open_session_stream(client, "ses_1")
    await _drain(frames)

    assert response.closed is True


@pytest.mark.asyncio
async def test_an_upstream_error_raises_before_any_response_is_started() -> None:
    """Eagerness is the point of this being a function rather than a generator.

    Starlette commits the 200 status line before pulling the first chunk of a
    ``StreamingResponse``, so a lazily-opened stream would turn a 404 for
    somebody else's session into a 200 with a silent empty body — much worse for
    an authorization check to degrade into than an error.
    """
    client, _ = _client(raises=NotFoundError("Session"))

    with pytest.raises(NotFoundError):
        await open_session_stream(client, "ses_1")


def test_the_default_delta_opt_in_is_the_buffered_events_preview() -> None:
    """``agent.message`` deltas render text as it generates; the buffered event
    still arrives and stays authoritative."""
    assert DELTA_EVENTS == ["agent.message"]
