"""``reembed_skills`` job — refresh ``skills.embedding`` after a model change.

The counterpart to ``brain_index_backfill`` for the *other* vector column. Both
tables must be migrated together: ``skills.embedding`` backs the boundary
classifier and ``query_brain``'s skill match, ``brain_chunks.embedding`` backs
brain-chat retrieval, and a model change invalidates both.

Why this exists at all: a vector is only comparable to vectors from the same
model. Mixing them doesn't raise — pgvector returns a plausible cosine either way
— so a half-migrated table degrades retrieval silently. ``embedding_model``
(migration 0018) makes staleness a column predicate, and this job drains it.

Properties (mirrors ``brain_index_backfill``):

* **Idempotent + resumable** — selects only skills whose vector wasn't produced by
  the configured model, so a re-run picks up exactly where a timeout or crash left
  off. Running it when nothing is stale is a no-op costing one SELECT.
* **Two-phase** — reads the stale set in one short transaction, embeds
  connectionless (network I/O never pins a pooled connection), writes in a second.
  A single skill's failure is logged and skipped, never aborting the batch: a
  partial migration is resumable, an aborted one wastes every embedding before it.
* **Tenant-scoped** — runs as ``system``/``admin`` inside ``run_in_tenant`` on the
  restricted pool, so RLS keeps every write inside ``workspace_id``.

Trigger it per workspace after changing ``settings.embedding_model``, together
with the brain index (both columns, or retrieval is left half-migrated)::

    from app.jobs.queue import enqueue
    await enqueue("reembed_skills", workspace_id)
    await enqueue("brain_index_backfill", workspace_id)

``force=True`` re-embeds every non-deleted skill regardless of provenance — for a
change to the *embedded text* (``embedder.skill_embedding_text``) rather than to
the model, which provenance alone can't detect.
"""
from __future__ import annotations

import logging

from app.config.database import get_tenant_session
from app.modules.skills.repository import SkillsRepository
from app.pipeline import embedder
from app.shared.middleware.with_tenant import run_in_tenant

log = logging.getLogger(__name__)

_repo = SkillsRepository()


async def reembed_skills(ctx: dict, workspace_id: str, *, force: bool = False) -> dict:
    """ARQ entrypoint. ``ctx`` is the ARQ job context (unused)."""
    model = embedder.current_model()

    # Phase 1: read the stale set (short transaction, no network I/O).
    async with get_tenant_session() as session:
        async with run_in_tenant(session, workspace_id, "system", "admin"):
            rows = await _repo.list_stale_embeddings(
                session, embedding_model=model, force=force
            )
    if not rows:
        log.info("reembed_skills: %s already current on %s", workspace_id, model)
        return {"reembedded": 0, "failed": 0, "embeddingModel": model, "forced": force}

    # Phase 2: embed OUTSIDE any transaction (network I/O must not pin a connection).
    embedded: list[tuple[str, list[float], str]] = []
    failed = 0
    for row in rows:
        text = embedder.skill_embedding_text(row["trigger"], row["base_logic"])
        if not text.strip():
            # Nothing to embed (no trigger and no logic) — leave the row's vector
            # alone rather than storing one for the empty string, which would match
            # every query equally badly.
            continue
        try:
            vector, usage = await embedder.embed_text(text)
        except Exception:  # noqa: BLE001 — one bad row must not abort the migration
            failed += 1
            log.warning(
                "reembed_skills: %s failed to embed skill %s", workspace_id, row["id"],
                exc_info=True,
            )
            continue
        embedded.append((row["id"], vector, usage.model))

    # Phase 3: write the refreshed vectors + their provenance.
    async with get_tenant_session() as session:
        async with run_in_tenant(session, workspace_id, "system", "admin"):
            for skill_id, vector, used_model in embedded:
                await _repo.set_embedding(
                    session, skill_id, embedding=vector, embedding_model=used_model
                )
            await session.commit()

    log.info(
        "reembed_skills: %s re-embedded %d/%d skills with %s (%d failed, force=%s)",
        workspace_id, len(embedded), len(rows), model, failed, force,
    )
    return {
        "reembedded": len(embedded),
        "failed": failed,
        "embeddingModel": model,
        "forced": force,
    }
