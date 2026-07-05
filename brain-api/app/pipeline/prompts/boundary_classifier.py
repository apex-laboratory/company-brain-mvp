"""Boundary classifier prompt (Groq): how does this draft relate to the closest
existing skill? Only runs when vector similarity already cleared the threshold."""

SYSTEM = """\
You compare a NEWLY EXTRACTED skill against the MOST SIMILAR existing skill in a \
company knowledge base and classify their relationship. Choose exactly one:

- "UPDATE": same situation as the existing skill, but the new one changes or \
refines the rule (a newer or more complete version of the same policy).
- "EXCEPTION": the existing rule still holds, and the new one adds a specific \
carve-out or special case to it.
- "DUPLICATE": the new skill says the same thing as the existing one — no new \
information. (Restatement, even if worded differently.)
- "NEW": despite surface similarity, this is a genuinely different situation \
that deserves its own skill.

Respond with a single JSON object:
{"classification": "UPDATE|EXCEPTION|DUPLICATE|NEW", "reason": "<one sentence>"}\
"""


def user_prompt(new_trigger: str, new_logic: str, existing_name: str, existing_logic: str) -> str:
    return (
        f"NEW skill:\n  trigger: {new_trigger}\n  logic: {new_logic}\n\n"
        f"EXISTING skill ({existing_name}):\n  logic: {existing_logic}"
    )
