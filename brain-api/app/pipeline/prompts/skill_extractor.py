"""Skill extractor prompt (Sonnet, Pass 2): structured skill draft from
decision moments + context."""

SYSTEM = """\
You extract structured, executable skills for a company knowledge base. Given \
the decision moments identified in a piece of workplace content (plus the \
surrounding context), produce ONE skill: a reusable description of how this \
company handles a specific situation.

Rules:
- Extract only what the content actually says. NEVER invent thresholds, \
names, steps, or conditions. If something is unclear or partially stated, \
say so in "uncertainty_notes" rather than guessing.
- When sources in the context conflict, prefer the statement with higher \
source authority (given below), and note the conflict in "uncertainty_notes".
- "trigger" is the situation that makes this skill apply, phrased so an AI \
agent could match it ("customer requests refund after 30 days").
- "base_logic" is the decision rule/process, step by step, in plain language.
- "exceptions" are explicit carve-outs stated in the content: \
[{"condition": "...", "action": "..."}].
- "actions" are concrete steps an agent could take: \
[{"description": "...", "system": "<tool/system if named>"}].
- "extraction_confidence" is your honest 0.0-1.0 self-assessment of how \
completely and unambiguously the content supports this skill.
- "name" is a short unique title (max 8 words) for the skill.

Respond with a single JSON object:
{"name": "...", "trigger": "...", "base_logic": "...",
 "exceptions": [...], "actions": [...],
 "extraction_confidence": 0.0, "uncertainty_notes": "..."}\
"""


def user_prompt(
    decisions_block: str, context: str, authority_tier: str, authority_weight: float
) -> str:
    return (
        f"Source authority: {authority_tier} (weight {authority_weight})\n\n"
        f"Decision moments:\n{decisions_block}\n\n"
        f"Full context:\n{context}"
    )
