from . import ExpandedContext


async def expand(event_payload: dict) -> ExpandedContext:
    """
    Fetch full surrounding context for a Jira event.
    Fetches: ticket body + all comments + linked tickets + transition log.
    """
    raise NotImplementedError("Phase 2")
