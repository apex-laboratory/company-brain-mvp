"""Contradiction detector prompt (Sonnet): does the proposed change actually
conflict with the current published rule (vs merely refining it)?"""

SYSTEM = """\
You decide whether a PROPOSED change to a company policy CONTRADICTS the \
CURRENT policy. A contradiction means the two cannot both be true at once — \
following one would violate the other (e.g. "refund within 30 days" vs \
"refunds are never given after 14 days").

It is NOT a contradiction when the proposed change merely adds detail, narrows \
scope, or updates a value in a compatible way. Be strict: only flag a genuine \
conflict a human must resolve.

Respond with a single JSON object:
{"has_contradiction": true|false, "reason": "<one sentence>"}\
"""


def user_prompt(proposed_logic: str, current_logic: str) -> str:
    return f"CURRENT policy:\n{current_logic}\n\nPROPOSED change:\n{proposed_logic}"
