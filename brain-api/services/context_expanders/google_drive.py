from . import ExpandedContext


async def expand(event_payload: dict) -> ExpandedContext:
    """
    Fetch full surrounding context for a Google Drive event.
    Fetches: full document text (Docs/Sheets/Slides exported to text) +
    parent folder name + document comments.
    """
    raise NotImplementedError("Phase 2")
