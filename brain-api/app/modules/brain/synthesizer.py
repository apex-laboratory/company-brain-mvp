"""Grounded answer synthesis — the "G" in RAG (BRAIN_CHAT_RAG_PLAN Phase 1).

Retrieval already found the relevant human-reviewed skills; this turns them into a
cited, confidence-scored answer. It reuses ``sonnet_json`` (claude-sonnet-5, temp
0.0, JSON-instructed with one reprompt), so a malformed reply self-heals once.

Anti-hallucination is the whole game here:

* The model answers **only** from the supplied skill bodies + provenance facts and
  must set ``grounded=false`` when the context does not actually answer — the
  service then downgrades to an honest no-match rather than shipping a guess.
* Governance facts (who approved / who originated / when it changed) are passed as
  an authoritative block the model may **quote but never infer**; an unrecorded
  field stays unrecorded, never back-filled with a plausible name or date.
* Superseded version history (Phase 2) is passed clearly labeled: usable only for
  "what changed / what did it used to be" questions, never stated as the current
  rule.
* Confidence is capped at the retrieval similarity, so the number can never exceed
  how well the question actually matched a skill.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from app.modules.brain.stream_parser import GroundedAnswerParser
from app.pipeline.llm.clients import sonnet_json, sonnet_stream
from app.pipeline.types import StageUsage

_STAGE = "brain_synthesis"

_SYSTEM = """\
You are the Company Brain: you answer operational questions for staff using ONLY \
the organization's human-reviewed skills (policies/decisions), their source \
material (evidence), and the provenance facts provided below.

Hard rules:
- Answer ONLY from the SKILLS, their SOURCE MATERIAL, PREVIOUS VERSIONS, and \
PROVENANCE given in the user message. Never use outside knowledge or invent policy.
- If none of the provided context actually answers the question, set "grounded" to \
false and give a short answer saying you don't have a reviewed skill covering it. \
Do not guess.
- Cite the skill id(s) you actually used in "usedSkillIds".
- SOURCE MATERIAL is the original message/doc that led to a skill. Use it to answer \
"who said / where did this come from" questions — attribute quotes to the given \
author and location, and never fabricate an author or location that isn't provided.
- For structured who-approved/when-changed facts, use ONLY the PROVENANCE block. If \
a provenance field is null or absent, say it is not recorded — never fabricate a \
name, date, or source.
- PREVIOUS VERSIONS (if present) are SUPERSEDED history. Use them ONLY to answer \
"what changed / what did it used to be" — never present a previous version as the \
current rule.
- Be concise and direct. Prefer the source's own wording.

Respond with a single JSON object, no prose around it. Emit the keys in EXACTLY \
this order — "answer" MUST come last:
{
  "grounded": <true|false>,
  "confidence": <number 0.0-1.0: how well the skills answer the question>,
  "usedSkillIds": ["skl_..."],
  "answer": "<the answer, or an honest 'I don't have a reviewed skill for that'>"
}
"""


def _render_skill(skill: dict[str, Any]) -> str:
    exceptions = skill.get("exceptions_block") or []
    exc_lines = "\n".join(
        f"    - {e.get('condition', '')}: {e.get('override') or e.get('action', '')}"
        for e in exceptions
    ) or "    (none)"
    return (
        f"- id: {skill.get('id')}\n"
        f"  name: {skill.get('name')}\n"
        f"  version: {skill.get('version')}\n"
        f"  authority: {skill.get('source_authority') or 'unknown'}\n"
        f"  confidence: {skill.get('confidence')}\n"
        f"  trigger: {(skill.get('trigger') or '').strip()}\n"
        f"  baseLogic: {(skill.get('base_logic') or '').strip()}\n"
        f"  exceptions:\n{exc_lines}"
    )


def _render_provenance(dossier: dict[str, Any] | None) -> str:
    """The governance block the model may quote but must never infer."""
    if not dossier:
        return "(no provenance recorded)"
    return json.dumps(dossier, default=str, indent=2)


def _render_history(history: list[dict[str, Any]] | None) -> str:
    """Superseded version bodies, labeled so they can't be stated as current."""
    if not history:
        return "(none)"
    return "\n".join(
        f"- skill {h.get('skill_id')} version {h.get('version')} (SUPERSEDED): "
        f"{(h.get('content') or '').strip()}"
        for h in history
    )


def _render_evidence(evidence: list[dict[str, Any]] | None) -> str:
    """Original source material with attribution — the basis for 'who said / which
    document' answers. Names the author when known (a message) and always names the
    source document (e.g. the Notion page) so the model can point at it."""
    if not evidence:
        return "(none)"
    lines = []
    for e in evidence:
        provider = e.get("provider") or "source"
        doc = e.get("location")
        author = e.get("author")
        doc_part = f" the {provider} document \"{doc}\"" if doc else f" a {provider} source"
        who = f"{author} in" if author else "from"
        lines.append(
            f"- {who}{doc_part} (skill {e.get('skill_id')}): "
            f"{(e.get('content') or '').strip()}"
        )
    return "\n".join(lines)


def _clamp01(value: Any, *, default: float = 0.0) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def _build_user(
    question: str,
    skills: list[dict[str, Any]],
    provenance: dict[str, Any] | None,
    history: list[dict[str, Any]] | None,
    evidence: list[dict[str, Any]] | None,
) -> str:
    """The user message — identical for the buffered and streamed paths."""
    skills_block = "\n".join(_render_skill(s) for s in skills) or "(none)"
    return (
        f"QUESTION:\n{question}\n\n"
        f"SKILLS (the current rules):\n{skills_block}\n\n"
        f"SOURCE MATERIAL (evidence — attribute quotes to the given author/location):\n"
        f"{_render_evidence(evidence)}\n\n"
        f"PREVIOUS VERSIONS (superseded — only for 'what changed' questions):\n"
        f"{_render_history(history)}\n\n"
        f"PROVENANCE (authoritative governance facts for the primary skill; "
        f"quote, never infer):\n{_render_provenance(provenance)}"
    )


def _score(head: dict[str, Any], top_similarity: float) -> tuple[bool, int, list[str]]:
    """Grounding verdict + honest confidence + cited ids, from the pre-answer keys.

    Confidence blends retrieval with the model's self-check and is capped at the
    retrieval similarity, so it can never exceed how well the question matched. An
    ungrounded answer scores 0.
    """
    grounded = bool(head.get("grounded", False))
    model_conf = _clamp01(head.get("confidence"), default=top_similarity)
    confidence = int(round(min(top_similarity, model_conf) * 100)) if grounded else 0
    used = head.get("usedSkillIds") or []
    used_ids = [str(s) for s in used if isinstance(s, (str, int))]
    return grounded, confidence, used_ids


async def answer(
    question: str,
    skills: list[dict[str, Any]],
    *,
    top_similarity: float,
    provenance: dict[str, Any] | None = None,
    history: list[dict[str, Any]] | None = None,
    evidence: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Synthesize a grounded answer over ``skills`` and their ``evidence``.

    ``top_similarity`` (cosine, 0-1) caps the returned confidence so it can never
    exceed how well the question matched. ``history`` carries superseded version
    bodies for "what changed" questions; ``evidence`` carries the original source
    material (attributed messages/docs) for "who said" questions. Returns
    ``{answer, grounded, used_skill_ids, confidence}`` where ``confidence`` is an
    int 0-100. Must be called OUTSIDE any open DB transaction (it does network I/O).
    """
    user = _build_user(question, skills, provenance, history, evidence)
    parsed, _usage = await sonnet_json(_SYSTEM, user, stage=_STAGE, interactive=True)
    grounded, confidence, used_ids = _score(parsed, top_similarity)
    return {
        "answer": str(parsed.get("answer") or "").strip(),
        "grounded": grounded,
        "used_skill_ids": used_ids,
        "confidence": confidence,
    }


async def answer_stream(
    question: str,
    skills: list[dict[str, Any]],
    *,
    top_similarity: float,
    provenance: dict[str, Any] | None = None,
    history: list[dict[str, Any]] | None = None,
    evidence: list[dict[str, Any]] | None = None,
) -> AsyncIterator[tuple[str, Any]]:
    """Streaming twin of :func:`answer`, yielding ``(kind, payload)`` events.

    * ``("head", {grounded, confidence, used_skill_ids})`` — the verdict, emitted as
      soon as the pre-answer keys arrive and always *before* any text.
    * ``("delta", str)`` — a piece of the answer. Emitted **only** when grounded, so
      an ungrounded reply never shows text the caller would have to retract.
    * ``("result", {...})`` — the same dict :func:`answer` returns.

    Network I/O: call OUTSIDE any open DB transaction.
    """
    user = _build_user(question, skills, provenance, history, evidence)
    parser = GroundedAnswerParser()
    grounded = False
    head_seen = False

    async for piece in sonnet_stream(_SYSTEM, user, stage=_STAGE):
        if isinstance(piece, StageUsage):
            continue
        delta = parser.feed(piece)
        if not head_seen and parser.head is not None:
            head_seen = True
            grounded, confidence, used_ids = _score(parser.head, top_similarity)
            yield "head", {
                "grounded": grounded,
                "confidence": confidence,
                "used_skill_ids": used_ids,
            }
        if delta and grounded:
            yield "delta", delta

    grounded, confidence, used_ids = _score(parser.head or {}, top_similarity)
    yield "result", {
        "answer": parser.answer.strip(),
        "grounded": grounded,
        "used_skill_ids": used_ids,
        "confidence": confidence,
    }
