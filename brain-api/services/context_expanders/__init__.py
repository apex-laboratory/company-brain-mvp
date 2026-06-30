from __future__ import annotations
from dataclasses import dataclass, field
from .slack import expand as _expand_slack
from .notion import expand as _expand_notion
from .github import expand as _expand_github
from .jira import expand as _expand_jira
from .zendesk import expand as _expand_zendesk
from .google_drive import expand as _expand_google_drive


@dataclass
class ExpandedContext:
    full_text: str
    author: str
    timestamp: str
    source_url: str
    metadata: dict = field(default_factory=dict)


_EXPANDERS = {
    "slack": _expand_slack,
    "notion": _expand_notion,
    "github": _expand_github,
    "jira": _expand_jira,
    "zendesk": _expand_zendesk,
    "google_drive": _expand_google_drive,
}


async def expand(source: str, event_payload: dict) -> ExpandedContext:
    """Dispatch context expansion to the correct source handler."""
    expander = _EXPANDERS.get(source)
    if expander is None:
        raise ValueError(f"No context expander registered for source: {source!r}")
    return await expander(event_payload)
