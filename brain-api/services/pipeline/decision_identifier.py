from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class DecisionMoment:
    message_id: str
    author: str
    timestamp: str
    decision_text: str
    signals: list[str] = field(default_factory=list)
    # e.g. ["definitive_language", "pinned", "positive_reactions", "authority_author"]


async def identify_decisions(content: str, source: str) -> list[DecisionMoment]:
    """
    Groq Pass 1: extract authoritative decision moments from threaded content.

    Signals it looks for:
      - Definitive language: "going forward", "the policy is", "confirmed:", "we've decided"
      - Positive reactions from multiple people (✅, 👍) indicating consensus
      - Pinned messages (always authoritative)
      - @channel / @here announcements in policy channels
      - Messages from designated authorities
      - Recency among debated messages (later messages resolve earlier disagreement)

    For non-threaded sources (Notion pages, GitHub files): wraps full content
    as a single DecisionMoment, skipping thread analysis.
    """
    raise NotImplementedError("Phase 3")
