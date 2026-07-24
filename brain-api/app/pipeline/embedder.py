"""OpenAI embedding wrapper (ported from ``services/embedder.py`` spec stub).

Embeds skill ``trigger + base_logic`` at write time; Phase 5 reuses it for
agent-query embedding at retrieval time. Dimension is pinned to the
``skills.embedding vector(1536)`` column — a model change that alters the
dimension must ship with a migration, so we assert it here.
"""
from __future__ import annotations

from typing import Any

from app.config.settings import settings
from app.pipeline.llm.pricing import cost_usd
from app.pipeline.llm.retry import with_retries
from app.pipeline.types import StageUsage

EMBEDDING_DIM = 1536  # must match skills.embedding vector(1536)

_openai_client: Any = None


def current_model() -> str:
    """The model new vectors are being produced by, for the ``embedding_model``
    provenance column (migration 0018). Read at call time, never cached, so a
    rotation takes effect without a restart."""
    return settings.embedding_model


def skill_embedding_text(trigger: str | None, base_logic: str | None) -> str:
    """The canonical text a skill's vector is built from: ``trigger\\nbase_logic``.

    Centralized because a re-embed must reproduce *exactly* what the write path
    embedded — same text, or the new vector lands somewhere else in the space and
    the tuned thresholds stop meaning what they meant. This is the form used by
    the extraction pipeline (``orchestrator``) and by review approve/write.
    """
    return "\n".join(t for t in (trigger, base_logic) if t)


def openai_client() -> Any:
    """Lazy ``AsyncOpenAI`` singleton."""
    global _openai_client
    if _openai_client is None:
        from openai import AsyncOpenAI

        if not settings.openai_api_key:
            raise RuntimeError(
                "OPENAI_API_KEY is not configured — the extraction pipeline needs "
                "it for embeddings. Set it in brain-api/.env."
            )
        _openai_client = AsyncOpenAI(api_key=settings.openai_api_key)
    return _openai_client


async def embed_text(text: str) -> tuple[list[float], StageUsage]:
    """Embed ``text`` → 1536-dim vector + usage, with transient retry."""

    async def _call():
        return await openai_client().embeddings.create(
            model=settings.embedding_model, input=text
        )

    resp = await with_retries(_call, stage="embedder")
    vector = resp.data[0].embedding
    if len(vector) != EMBEDDING_DIM:
        raise RuntimeError(
            f"embedding dimension {len(vector)} != {EMBEDDING_DIM}; "
            f"model {settings.embedding_model!r} needs a schema migration"
        )
    tokens = resp.usage.prompt_tokens if resp.usage else 0
    usage = StageUsage(
        stage="embedder",
        model=settings.embedding_model,
        input_tokens=tokens,
        output_tokens=0,
        cost_usd=cost_usd(settings.embedding_model, tokens, 0),
    )
    return vector, usage
