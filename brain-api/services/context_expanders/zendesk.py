from . import ExpandedContext


async def expand(event_payload: dict) -> ExpandedContext:
    """
    Fetch full surrounding context for a Zendesk event.
    Fetches: ticket + all comments + tags + resolution note.
    """
    raise NotImplementedError("Phase 2")
