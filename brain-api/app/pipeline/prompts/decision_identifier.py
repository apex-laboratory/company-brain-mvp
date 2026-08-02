"""Decision identifier prompt (Gemini, Pass 1): structure threaded content into
authoritative decision moments."""

SYSTEM = """\
You are analyzing a workplace conversation thread (Slack thread, support \
ticket, or code-review discussion). Identify the DECISION MOMENTS: messages \
where someone states an authoritative rule, policy, resolution, or process \
decision — not proposals, questions, or speculation.

Authority signals to look for (report the ones that apply):
- "definitive_language": stated as a fact or instruction ("we always...", "policy is...")
- "pinned": the message is pinned or marked as an answer
- "positive_reactions": affirming reactions (checkmarks, +1s) from others
- "authority_author": author is a lead/owner/admin in this context
- "resolution": the message closes the question/ticket

Respond with a single JSON object:
{"decisions": [
  {"message_id": "<id or index>",
   "author": "<name or id>",
   "timestamp": "<as given>",
   "decision_text": "<the decision, quoted or tightly paraphrased>",
   "signals": ["definitive_language", ...]}
]}

Return {"decisions": []} if the thread contains no authoritative decision.\
"""


def user_prompt(content: str, provider: str) -> str:
    return f"Source: {provider}\n\nThread:\n{content}"
