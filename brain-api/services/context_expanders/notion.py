from . import ExpandedContext


async def expand(event_payload: dict) -> ExpandedContext:
    """
    Fetch full surrounding context for a Notion event.
    Fetches: full page body + parent page title + linked page titles and excerpts.
    """
    raise NotImplementedError("Phase 2")
