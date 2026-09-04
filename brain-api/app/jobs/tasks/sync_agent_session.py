"""``sync_agent_session`` job — fold one Anthropic webhook into the mirror.

Queued rather than done inline, for the reason every webhook in this repo is
queued: the handler has to answer fast or the vendor retries, and this work
includes a round-trip back to Anthropic to read the session's usage (the delivery
carries only ``{id, type}``). Doing that inside the request would make our
response time a function of the vendor's, on a call the vendor is timing.

**Nothing here reads session content.** It updates status, stop reason and token
counts — the columns migration 0028 has — and there is no path through it that
touches an event.
"""
from __future__ import annotations

import logging

from app.modules.agents.sessions import AgentSessionsService

log = logging.getLogger(__name__)

_service = AgentSessionsService()


async def sync_agent_session(
    ctx: dict, anthropic_session_id: str, event_type: str
) -> dict[str, bool]:
    """ARQ entrypoint. Idempotent: the same delivery twice is the same write twice.

    Every field the mirror takes is either COALESCE'd or set from the session's
    current state, so a redelivery converges rather than accumulating. That
    matters because ARQ's job-id dedupe only holds for as long as the result is
    kept, and Anthropic retries on its own schedule.
    """
    landed = await _service.sync_from_webhook(
        anthropic_session_id, event_type=event_type
    )
    if not landed:
        # Not ours. One Anthropic organization can serve several deployments of
        # this app, so this is an ordinary outcome, logged at debug rather than
        # as a warning that would cry wolf on every staging delivery.
        log.debug("agent session %s is not this deployment's", anthropic_session_id)
    return {"synced": landed}
