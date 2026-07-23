"""Brain chat schemas (BACKEND_ASKS §7, BRAIN_CHAT_RAG_PLAN).

Responses serialize camelCase via the shared ``CamelModel``; requests reject
unknown keys via ``CamelRequestModel``.
"""
from __future__ import annotations

from app.shared.schemas import CamelModel


class BrainStatusResponse(CamelModel):
    """Readiness of the "Ask the brain" surface for the caller's workspace.

    Lets the dashboard grey out the chat (and explain why) *without* first firing
    a query that would 409. ``reason`` is ``null`` when ready, else one of
    ``disabled`` (global kill-switch) / ``no_skills`` (nothing indexed yet).
    """

    enabled: bool
    ready: bool
    skills_indexed: int
    reason: str | None = None
