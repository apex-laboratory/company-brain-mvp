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
* Confidence is capped at the retrieval similarity, so the number can never exceed
  how well the question actually matched a skill.
"""
from __future__ import annotations

import json
from typing import Any

from app.pipeline.llm.clients import sonnet_json

_STAGE = "brain_synthesis"

_SYSTEM = """\
You are the Company Brain: you answer operational questions for staff using ONLY \
the organization's human-reviewed skills (policies/decisions) and the provenance \
facts provided below.

Hard rules:
- Answer ONLY from the SKILLS and PROVENANCE given in the user message. Never use \
outside knowledge or invent policy.
- If the provided skills do not actually answer the question, set "grounded" to \
false and give a short answer saying you don't have a reviewed skill covering it. \
Do not guess.
- Cite the skill id(s) you actually used in "usedSkillIds".
- For any who/when/where-it-came-from question, use ONLY the PROVENANCE block. If a \
provenance field is null or absent, say it is not recorded — never fabricate a \
name, date, or source.
- Be concise and direct. Prefer the skill's own wording.

Respond with a single JSON object, no prose around it:
{
  "answer": "<the answer, or an honest 'I don't have a reviewed skill for that'>",
  "grounded": <true|false>,
  "usedSkillIds": ["skl_..."],
  "confidence": <number 0.0-1.0: how well the skills answer the question>
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


def _clamp01(value: Any, *, default: float = 0.0) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


async def answer(
    question: str,
    skills: list[dict[str, Any]],
    *,
    top_similarity: float,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Synthesize a grounded answer over ``skills``.

    ``top_similarity`` (cosine, 0-1) caps the returned confidence so it can never
    exceed how well the question matched. Returns
    ``{answer, grounded, used_skill_ids, confidence}`` where ``confidence`` is an
    int 0-100. Must be called OUTSIDE any open DB transaction (it does network I/O).
    """
    skills_block = "\n".join(_render_skill(s) for s in skills)
    user = (
        f"QUESTION:\n{question}\n\n"
        f"SKILLS (the only knowledge you may use):\n{skills_block}\n\n"
        f"PROVENANCE (authoritative governance facts for the primary skill; "
        f"quote, never infer):\n{_render_provenance(provenance)}"
    )

    parsed, _usage = await sonnet_json(_SYSTEM, user, stage=_STAGE)

    grounded = bool(parsed.get("grounded", False))
    model_conf = _clamp01(parsed.get("confidence"), default=top_similarity)
    # Confidence blends retrieval + the model's self-check and is capped at the
    # retrieval similarity, so it stays honest. An ungrounded answer scores 0.
    confidence = int(round(min(top_similarity, model_conf) * 100)) if grounded else 0

    used = parsed.get("usedSkillIds") or []
    used_ids = [str(s) for s in used if isinstance(s, (str, int))]

    return {
        "answer": str(parsed.get("answer") or "").strip(),
        "grounded": grounded,
        "used_skill_ids": used_ids,
        "confidence": confidence,
    }
