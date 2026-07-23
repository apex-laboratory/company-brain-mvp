"""``brain_index_backfill`` job — index skills into ``brain_chunks`` (P2 + P3).

The "ingest all versioned skills + their evidence" backfill. In one pass it indexes:

* **Skill versions** (P2) — every published skill + every historical
  ``skill_versions`` body as ``kind='skill_version'``, then re-syncs ``is_current``
  so exactly the live version is current and superseded ones are demoted.
* **Evidence** (P3) — each skill's deciding message (``reviews.evidence_quote`` +
  author + channel/doc) as ``kind='evidence'``, so "whose message said we need this"
  is answerable from the source material, attributed and skill-linked.

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
from app.modules.brain.authors import resolve_author
from app.modules.brain.chunking import chunk_text
from app.modules.brain.repository import BrainRepository, chunk_key
from app.pipeline import embedder
from app.shared.middleware.with_tenant import run_in_tenant

log = logging.getLogger(__name__)

_repo = BrainRepository()


async def brain_index_backfill(ctx: dict, workspace_id: str) -> dict:
    """ARQ entrypoint. ``ctx`` is the ARQ job context (unused)."""
    # Phase 1: read what to index (skill versions + evidence) + what's already indexed.
    async with get_tenant_session() as session:
        async with run_in_tenant(session, workspace_id, "system", "admin"):
            version_rows = await _repo.list_version_index_rows(session)
            evidence_rows = await _repo.list_evidence_rows(session)
            existing = await _repo.list_chunk_keys(session)  # all kinds

    version_plan = _plan_version_chunks(version_rows, existing)
    evidence_plan = _plan_evidence_chunks(evidence_rows, existing)

    # Phase 2: embed the new chunks OUTSIDE any transaction (network I/O).
    for item in (*version_plan, *evidence_plan):
        vector, _ = await embedder.embed_text(item["content"])
        item["embedding"] = vector

    # Phase 3: upsert new chunks + re-sync is_current (promote live, demote prior).
    async with get_tenant_session() as session:
        async with run_in_tenant(session, workspace_id, "system", "admin"):
            for item in version_plan:
                await _repo.upsert_skill_version_chunk(
                    session, workspace_id=workspace_id,
                    skill_id=item["skill_id"], version=item["version"],
                    is_current=item["is_current"], chunk_index=item["chunk_index"],
                    chunk_key=item["chunk_key"], content=item["content"],
                    embedding=item["embedding"],
                )
            for item in evidence_plan:
                await _repo.upsert_evidence_chunk(
                    session, workspace_id=workspace_id,
                    skill_id=item["skill_id"], source_ref=item["source_ref"],
                    chunk_index=item["chunk_index"], chunk_key=item["chunk_key"],
                    content=item["content"], embedding=item["embedding"],
                )
            await _repo.sync_skill_version_current(session)
            await session.commit()

    indexed = len(version_plan) + len(evidence_plan)
    log.info(
        "brain_index_backfill: %s indexed %d new chunks (%d version, %d evidence)",
        workspace_id, indexed, len(version_plan), len(evidence_plan),
    )
    return {
        "indexed": indexed,
        "versionChunks": len(version_plan),
        "evidenceChunks": len(evidence_plan),
    }


def _plan_version_chunks(rows: list[dict], existing: set[str]) -> list[dict]:
    plan: list[dict] = []
    for row in rows:
        for idx, content in enumerate(chunk_text(row["base_logic"])):
            key = chunk_key("skill_version", row["skill_id"], row["version"], idx)
            if key in existing:
                continue
            plan.append({
                "skill_id": row["skill_id"], "version": row["version"],
                "is_current": bool(row["is_current"]), "chunk_index": idx,
                "chunk_key": key, "content": content,
            })
    return plan


def _plan_evidence_chunks(rows: list[dict], existing: set[str]) -> list[dict]:
    plan: list[dict] = []
    for row in rows:
        author = resolve_author(row["provider"], row["author"])  # decision-F seam
        for idx, content in enumerate(chunk_text(row["content"])):
            key = chunk_key("evidence", row["skill_id"], row["review_id"], idx)
            if key in existing:
                continue
            plan.append({
                "skill_id": row["skill_id"], "chunk_index": idx, "chunk_key": key,
                "content": content,
                "source_ref": {
                    "provider": row["provider"],
                    "sourceItemId": row["review_id"],
                    "url": None,
                    "label": row["location"],
                    "author": author,
                },
            })
    return plan
