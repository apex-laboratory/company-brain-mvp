from . import ExpandedContext


async def expand(event_payload: dict) -> ExpandedContext:
    """
    Fetch full surrounding context for a Slack event.
    Fetches: full thread from message ID, including all replies and reactions.
    """
    raise NotImplementedError("Phase 2")
