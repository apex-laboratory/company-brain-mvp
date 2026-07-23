"""``brain_index_backfill`` job — index skill versions into ``brain_chunks`` (P2).

The "ingest all versioned skills, previous + new" backfill. For every published
skill and every historical ``skill_versions`` row, chunk → embed → upsert a
``kind='skill_version'`` chunk, then re-sync ``is_current`` so exactly the live
version is marked current and superseded ones are demoted.

Properties:

* **Idempotent** — upserts on ``(workspace_id, chunk_key)`` and skips already-indexed
  chunks, so a re-run only embeds what's new. Safe to fire on every publish (the
  keep-fresh hook) and to re-run at will.
* **Two-phase** — reads what needs indexing in one short transaction, embeds
  connectionless (network I/O never pins a pooled connection), writes in a second.
* **Tenant-scoped** — runs as ``system``/``admin`` inside ``run_in_tenant`` on the
  restricted pool, so RLS keeps every chunk in its workspace.
"""
from __future__ import annotations

import logging

from app.config.database import get_tenant_session
from app.modules.brain.chunking import chunk_text
from app.modules.brain.repository import BrainRepository, chunk_key
from app.pipeline import embedder
from app.shared.middleware.with_tenant import run_in_tenant

log = logging.getLogger(__name__)

_repo = BrainRepository()


async def brain_index_backfill(ctx: dict, workspace_id: str) -> dict:
    """ARQ entrypoint. ``ctx`` is the ARQ job context (unused)."""
    # Phase 1: read the (skill, version) bodies to index + what's already indexed.
    async with get_tenant_session() as session:
        async with run_in_tenant(session, workspace_id, "system", "admin"):
            rows = await _repo.list_version_index_rows(session)
            existing = await _repo.list_chunk_keys(session, kind="skill_version")

    # Build the chunk plan; only chunks whose key isn't indexed yet need embedding.
    plan: list[dict] = []
    for row in rows:
        chunks = chunk_text(row["base_logic"])
        for idx, content in enumerate(chunks):
            key = chunk_key("skill_version", row["skill_id"], row["version"], idx)
            plan.append(
                {
                    "skill_id": row["skill_id"],
                    "version": row["version"],
                    "is_current": bool(row["is_current"]),
                    "chunk_index": idx,
                    "chunk_key": key,
                    "content": content,
                }
            )
    to_embed = [p for p in plan if p["chunk_key"] not in existing]

    # Phase 2: embed the new chunks OUTSIDE any transaction (network I/O).
    for item in to_embed:
        vector, _ = await embedder.embed_text(item["content"])
        item["embedding"] = vector

    # Phase 3: upsert new chunks + re-sync is_current (promote live, demote prior).
    async with get_tenant_session() as session:
        async with run_in_tenant(session, workspace_id, "system", "admin"):
            for item in to_embed:
                await _repo.upsert_skill_version_chunk(
                    session, workspace_id=workspace_id,
                    skill_id=item["skill_id"], version=item["version"],
                    is_current=item["is_current"], chunk_index=item["chunk_index"],
                    chunk_key=item["chunk_key"], content=item["content"],
                    embedding=item["embedding"],
                )
            await _repo.sync_skill_version_current(session)
            await session.commit()

    log.info(
        "brain_index_backfill: %s indexed %d new / %d total skill-version chunks",
        workspace_id, len(to_embed), len(plan),
    )
    return {"indexed": len(to_embed), "total": len(plan)}
