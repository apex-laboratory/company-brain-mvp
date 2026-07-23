"""Brain chat schemas (BACKEND_ASKS §7, BRAIN_CHAT_RAG_PLAN).

Responses serialize camelCase via the shared ``CamelModel``; requests reject
unknown keys via ``CamelRequestModel``.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import Field

from app.shared.schemas import CamelModel, CamelRequestModel


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


class BrainQueryRequest(CamelRequestModel):
    """A dashboard/agent question. ``conversationId`` continues an existing thread;
    omit it to start a new one (dashboard JWT callers only — see the service)."""

    question: str = Field(min_length=1, max_length=2000)
    conversation_id: str | None = None


class SourceCitation(CamelModel):
    """One source the answer drew on. ``location`` is the channel/doc name; from
    Phase 3 the evidence graph fills ``url``/``excerpt``."""

    provider: str | None = None
    location: str | None = None
    skill_id: str | None = None
    url: str | None = None
    excerpt: str | None = None


class ProvenancePerson(CamelModel):
    """A governance actor in a skill's lineage. Every field is optional and returned
    ``null`` when the write path hasn't recorded it — never inferred by the model."""

    name: str | None = None
    at: datetime | None = None
    via: str | None = None
    location: str | None = None
    change_type: str | None = None


class BrainProvenance(CamelModel):
    """Structured governance dossier for the primary matched skill (the FE renders
    it; the synthesizer may quote it but never fabricates a field)."""

    approved_by: ProvenancePerson | None = None
    originated_by: ProvenancePerson | None = None
    created_by: ProvenancePerson | None = None
    last_edited_by: ProvenancePerson | None = None


class BrainQueryResponse(CamelModel):
    """The answer envelope (BACKEND_ASKS §7, enriched with trust + provenance).

    ``trust``: ``skill`` (a reviewed rule) | ``evidence`` (a cited source in a
    skill's lineage, Phase 3) | ``none`` (no reviewed skill covers it — an honest
    miss, never a guess). ``matchType`` mirrors the interaction log.
    """

    answer: str
    trust: str
    confidence: int
    match_type: str
    sources: list[SourceCitation] = Field(default_factory=list)
    skill_ids: list[str] = Field(default_factory=list)
    provenance: BrainProvenance | None = None
    conversation_id: str | None = None
    message_id: str | None = None
    interaction_id: str
