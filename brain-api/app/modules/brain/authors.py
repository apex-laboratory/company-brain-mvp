"""Author-identity resolution seam for evidence (BRAIN_CHAT_RAG_PLAN decision F).

Evidence carries whoever the extractor recorded as the source author. Four
providers already give a readable name (GitHub ``user.login``, Google Drive/Gmail
display name/email, Jira display name); **Slack** and **Zendesk** give a raw id
(``U07A3B12`` / ``380288``) and **Notion** usually gives none (a page, not a person).

Resolving those ids → names is connector-side work (Slack ``users.info``, Zendesk
Users API, Notion ``created_by`` + Users API) that needs each connector's token and
scopes — independent of the brain build. The plan sanctions shipping the evidence
graph first (it already surfaces id + quote + channel, which is useful) and landing
name resolution separately. This function is that seam: today it passes the value
through unchanged; decision F swaps in real resolution without touching capture or
retrieval.
"""
from __future__ import annotations


def resolve_author(provider: str | None, raw_author: str | None) -> str | None:
    """Best-effort display name for an evidence author.

    Currently returns ``raw_author`` verbatim (a real name for GitHub/Drive/Gmail/
    Jira; a raw id for Slack/Zendesk; ``None`` for Notion). Never invents a name —
    an unresolvable id stays an id so the answer is honest about what's recorded.
    """
    return raw_author
