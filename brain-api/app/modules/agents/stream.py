"""SSE pass-through for an agent session's event stream (agent-builder-plan §5.5).

**If a reviewer sees an accumulator variable in this file, that is the bug.** The
rule is the plan's and it is not stylistic: an agent session's events carry the
contents of files it read, the output of commands it ran, and whatever its
connectors returned. Holding any of that — even briefly, even only to re-serialize
it — moves connector data onto our infrastructure and voids the split the whole
feature exists to buy. Frames are forwarded one at a time and nothing survives the
loop iteration that produced it.

There is one distinction worth being precise about, because it looks like a
violation and is not. This proxy iterates **lines**, not raw bytes. Framing is
not accumulation: a line is one frame's worth of bytes that httpx was going to
buffer anyway, it is released the moment it is written downstream, and no list,
string or dict grows across iterations. What framing buys is §4.6's metadata tap.

**The tap can only ever see event names.** SSE frames from Anthropic arrive as an
``event:`` line followed by a ``data:`` line, so a tap fed from ``event:`` lines
alone is structurally incapable of reading content — not by convention, not by a
reviewer remembering, but because the bytes never reach it. That is the seam the
plan asks for, and it is the shape it should keep: when the envelope builder
lands, it gets event *types* and timings and must go on having no way to ask for
more.

**Deployment note.** This route holds a connection open for the length of an
agent's turn, which is minutes of silence between frames while a tool runs.
Anything terminating TLS in front of it needs its read timeout raised well past
the default (nginx's ``proxy_read_timeout`` is 60s) or working streams will be
severed mid-thought, and the client will read that disconnect as the agent having
stopped.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack

from app.modules.agents.anthropic_client import AnthropicAgentsClient

log = logging.getLogger(__name__)

# Opt into incremental previews so text renders as it generates. The buffered
# ``agent.message`` still arrives afterwards and stays authoritative — the delta
# is a preview, not the record. Note the delta type is ``content_delta``, *not*
# the Messages API's ``content_block_delta``; accumulator code from elsewhere in
# the codebase will not drop in.
DELTA_EVENTS = ["agent.message"]

_EVENT_FIELD = "event:"


async def open_session_stream(
    anthropic: AnthropicAgentsClient,
    anthropic_session_id: str,
    *,
    event_deltas: list[str] | None = None,
    on_event: Callable[[str], None] | None = None,
) -> AsyncIterator[bytes]:
    """Connect upstream **now** and return an iterator over its frames.

    The connection is opened before this returns, and that ordering is the whole
    point of the function. Starlette commits the ``200`` status line before it
    pulls the first chunk from a ``StreamingResponse``, so a stream that opened
    lazily would turn a 404 for an unknown session — or a 401 for a rejected key —
    into a 200 followed by a silent, empty body. Opening here means those still
    raise as ordinary ``AppError``s and render through the normal envelope.

    The exit stack is closed by the iterator's ``finally``, so the upstream socket
    dies with the downstream one: a browser that navigates away closes the
    generator, which closes the response, which closes the connection to
    Anthropic. Without that, a user who reloads a chat leaves the vendor writing
    frames into nothing for the rest of the turn.
    """
    stack = AsyncExitStack()
    response = await stack.enter_async_context(
        anthropic.stream_events(
            anthropic_session_id,
            event_deltas=event_deltas if event_deltas is not None else DELTA_EVENTS,
        )
    )

    async def _frames() -> AsyncIterator[bytes]:
        try:
            async for line in response.aiter_lines():
                # ``aiter_lines`` drops the terminator and yields "" for the blank
                # line that ends an SSE event, so re-adding a single "\n" rebuilds
                # a well-formed stream. It also normalizes CRLF to LF, which the
                # SSE grammar allows.
                if on_event is not None and line.startswith(_EVENT_FIELD):
                    _tap(on_event, line)
                yield f"{line}\n".encode()
        finally:
            await stack.aclose()

    return _frames()


def _tap(on_event: Callable[[str], None], line: str) -> None:
    """Hand the observer one event name, and never let it break the stream.

    A raising tap must not be able to sever a session — the stream is the
    product and the tap is telemetry, so the failure is swallowed and logged.
    Reversing that would let a bug in future envelope-building code look
    exactly like the agent crashing.
    """
    try:
        on_event(line[len(_EVENT_FIELD):].strip())
    except Exception:  # noqa: BLE001 — telemetry must never break the stream
        log.exception("agent stream: event tap raised; continuing")
