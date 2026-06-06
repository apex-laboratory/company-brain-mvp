from . import ExpandedContext


async def expand(event_payload: dict) -> ExpandedContext:
    """
    Fetch full surrounding context for a GitHub event.
    Fetches: PR description + all review comments + body of linked issues.
    """
    raise NotImplementedError("Phase 2")
