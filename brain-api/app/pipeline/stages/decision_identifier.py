"""Decision identifier (Groq, Pass 1): structure threaded content into
authoritative decision moments.

Per the spec stub: threaded providers (Slack threads, Zendesk ticket comment
chains, GitHub review discussions) get the LLM pass; non-threaded sources
(Notion pages, Drive docs, Gmail messages) wrap the full content as a single
DecisionMoment **without an LLM call** — there is no thread structure to
analyze, and the extractor sees the full content anyway.
"""
from __future__ import annotations

from app.pipeline.llm.clients import groq_json
from app.pipeline.prompts import decision_identifier as prompts
from app.pipeline.types import DecisionMoment, StageUsage

THREADED_PROVIDERS = frozenset({"slack", "zendesk", "github", "jira"})


def _moment_from(entry: dict) -> DecisionMoment:
    signals = entry.get("signals")
    return DecisionMoment(
        message_id=str(entry.get("message_id", "")),
        author=str(entry.get("author", "")),
        timestamp=str(entry.get("timestamp", "")),
        decision_text=str(entry.get("decision_text", "")),
        signals=[str(s) for s in signals] if isinstance(signals, list) else [],
    )


async def identify_decisions(
    content: str,
    provider: str,
    *,
    source_id: str,
    author: str,
    timestamp: str,
) -> tuple[list[DecisionMoment], StageUsage | None]:
    """Return ``(decision_moments, usage)``; usage is None on the no-LLM path.

    An empty list means "no authoritative decision found" — the caller discards
    the event.
    """
    if provider not in THREADED_PROVIDERS:
        return (
            [
                DecisionMoment(
                    message_id=source_id,
                    author=author,
                    timestamp=timestamp,
                    decision_text=content,
                    signals=[],
                )
            ],
            None,
        )

    parsed, usage = await groq_json(
        prompts.SYSTEM,
        prompts.user_prompt(content, provider),
        stage="decision_identifier",
        max_tokens=1024,
    )
    raw = parsed.get("decisions")
    moments = [
        _moment_from(e) for e in raw if isinstance(e, dict)
    ] if isinstance(raw, list) else []
    # Drop entries the model returned without any decision text.
    return [m for m in moments if m.decision_text.strip()], usage
