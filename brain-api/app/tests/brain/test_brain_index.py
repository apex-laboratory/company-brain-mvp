"""Brain index (Phase 2) tests — versioned ingestion into brain_chunks.

Covers the backfill job (idempotency, current-vs-historical labeling, tenant
scoping, is_current re-sync), the chunker, the keep-fresh hook, and the
synthesizer's superseded-history rendering. All mocked — no DB/Redis.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from app.jobs.tasks import brain_index_backfill as job_module
from app.modules.brain import reindex
from app.modules.brain.chunking import chunk_text
from app.modules.brain.repository import chunk_key
from app.pipeline.types import StageUsage


class _AsyncCtx:
    def __init__(self, value: object) -> None:
        self._value = value

    async def __aenter__(self) -> object:
        return self._value

    async def __aexit__(self, *_: object) -> bool:
        return False


def _rows() -> list[dict]:
    return [
        {"skill_id": "skl_1", "version": "v2", "base_logic": "refund 45 days", "is_current": True},
        {"skill_id": "skl_1", "version": "v1", "base_logic": "refund 30 days", "is_current": False},
    ]


def _evidence_rows() -> list[dict]:
    return [
        {"skill_id": "skl_1", "review_id": "rev_1", "provider": "slack",
         "location": "#cs-escalations", "author": "U07A3B12",
         "content": "we need a 45-day refund window for premium customers"},
    ]


def _repo_mock(*, version_rows=None, evidence_rows=None, existing=None) -> MagicMock:
    return MagicMock(
        list_version_index_rows=AsyncMock(return_value=version_rows if version_rows is not None else _rows()),
        list_evidence_rows=AsyncMock(return_value=evidence_rows if evidence_rows is not None else []),
        list_chunk_keys=AsyncMock(return_value=existing if existing is not None else set()),
        upsert_skill_version_chunk=AsyncMock(),
        upsert_evidence_chunk=AsyncMock(),
        sync_skill_version_current=AsyncMock(),
    )


def _job_patches(repo, session):
    return (
        patch.object(job_module, "_repo", repo),
        patch.object(job_module, "get_tenant_session", return_value=_AsyncCtx(session)),
        patch.object(job_module, "run_in_tenant", return_value=_AsyncCtx(None)),
        patch.object(
            job_module.embedder, "embed_text",
            AsyncMock(return_value=([0.0] * 1536, StageUsage("e", "m", 1, 0, 0.0))),
        ),
    )


# ── chunking ─────────────────────────────────────────────────────────────────

def test_chunk_text_short_and_empty() -> None:
    assert chunk_text("a short policy") == ["a short policy"]
    assert chunk_text("   ") == []
    assert chunk_text(None) == []


def test_chunk_text_long_splits() -> None:
    long = "This is a sentence. " * 300  # ~6000 chars
    chunks = chunk_text(long, max_chars=1500)
    assert len(chunks) > 1 and all(len(c) <= 1500 for c in chunks)


def test_chunk_key_is_deterministic() -> None:
    assert chunk_key("skill_version", "skl_1", "v2", 0) == "skill_version:skl_1:v2:0"


# ── backfill job ─────────────────────────────────────────────────────────────

async def test_backfill_indexes_current_and_historical_with_flags() -> None:
    repo = _repo_mock()
    session = MagicMock(commit=AsyncMock())
    patches = _job_patches(repo, session)
    for p in patches:
        p.start()
    try:
        result = await job_module.brain_index_backfill({}, "wrk_1")
    finally:
        for p in patches:
            p.stop()
    assert result == {"indexed": 2, "versionChunks": 2, "evidenceChunks": 0}
    assert repo.upsert_skill_version_chunk.await_count == 2
    repo.sync_skill_version_current.assert_awaited_once()  # promote live / demote prior
    by_version = {
        c.kwargs["version"]: c.kwargs["is_current"]
        for c in repo.upsert_skill_version_chunk.await_args_list
    }
    assert by_version == {"v2": True, "v1": False}


async def test_backfill_is_idempotent_skips_indexed_but_resyncs() -> None:
    existing = {chunk_key("skill_version", "skl_1", "v2", 0),
                chunk_key("skill_version", "skl_1", "v1", 0)}
    repo = _repo_mock(existing=existing)
    session = MagicMock(commit=AsyncMock())
    embed = AsyncMock(return_value=([0.0] * 1536, StageUsage("e", "m", 1, 0, 0.0)))
    with patch.object(job_module, "_repo", repo), \
         patch.object(job_module, "get_tenant_session", return_value=_AsyncCtx(session)), \
         patch.object(job_module, "run_in_tenant", return_value=_AsyncCtx(None)), \
         patch.object(job_module.embedder, "embed_text", embed):
        result = await job_module.brain_index_backfill({}, "wrk_1")
    assert result == {"indexed": 0, "versionChunks": 0, "evidenceChunks": 0}
    repo.upsert_skill_version_chunk.assert_not_awaited()  # nothing new to write
    embed.assert_not_awaited()                            # no embedding spend on re-run
    repo.sync_skill_version_current.assert_awaited_once()  # is_current still reconciled


async def test_backfill_scoped_to_caller_workspace() -> None:
    repo = _repo_mock()
    session = MagicMock(commit=AsyncMock())
    run_in_tenant = MagicMock(return_value=_AsyncCtx(None))
    with patch.object(job_module, "_repo", repo), \
         patch.object(job_module, "get_tenant_session", return_value=_AsyncCtx(session)), \
         patch.object(job_module, "run_in_tenant", run_in_tenant), \
         patch.object(job_module.embedder, "embed_text",
                      AsyncMock(return_value=([0.0] * 1536, StageUsage("e", "m", 1, 0, 0.0)))):
        await job_module.brain_index_backfill({}, "wrk_1")
    assert run_in_tenant.call_args_list  # opened at least once
    for call in run_in_tenant.call_args_list:
        assert call.args[1] == "wrk_1"  # every tenant block scoped to the caller's ws


# ── evidence capture (Phase 3) ───────────────────────────────────────────────

async def test_backfill_captures_evidence_with_attribution() -> None:
    repo = _repo_mock(version_rows=[], evidence_rows=_evidence_rows())
    session = MagicMock(commit=AsyncMock())
    patches = _job_patches(repo, session)
    for p in patches:
        p.start()
    try:
        result = await job_module.brain_index_backfill({}, "wrk_1")
    finally:
        for p in patches:
            p.stop()
    assert result == {"indexed": 1, "versionChunks": 0, "evidenceChunks": 1}
    repo.upsert_evidence_chunk.assert_awaited_once()
    kw = repo.upsert_evidence_chunk.await_args.kwargs
    assert kw["skill_id"] == "skl_1"
    assert kw["source_ref"] == {
        "provider": "slack", "sourceItemId": "rev_1", "url": None,
        "label": "#cs-escalations", "author": "U07A3B12",  # raw id kept (decision F)
    }
    assert kw["chunk_key"] == chunk_key("evidence", "skl_1", "rev_1", 0)


async def test_backfill_evidence_is_idempotent() -> None:
    existing = {chunk_key("evidence", "skl_1", "rev_1", 0)}
    repo = _repo_mock(version_rows=[], evidence_rows=_evidence_rows(), existing=existing)
    session = MagicMock(commit=AsyncMock())
    embed = AsyncMock(return_value=([0.0] * 1536, StageUsage("e", "m", 1, 0, 0.0)))
    with patch.object(job_module, "_repo", repo), \
         patch.object(job_module, "get_tenant_session", return_value=_AsyncCtx(session)), \
         patch.object(job_module, "run_in_tenant", return_value=_AsyncCtx(None)), \
         patch.object(job_module.embedder, "embed_text", embed):
        result = await job_module.brain_index_backfill({}, "wrk_1")
    assert result["evidenceChunks"] == 0
    repo.upsert_evidence_chunk.assert_not_awaited()
    embed.assert_not_awaited()


def test_resolve_author_passes_through() -> None:
    from app.modules.brain.authors import resolve_author
    assert resolve_author("github", "janedoe") == "janedoe"
    assert resolve_author("slack", "U07A3B12") == "U07A3B12"  # id kept, never invented
    assert resolve_author("notion", None) is None


# ── keep-fresh hook ──────────────────────────────────────────────────────────

async def test_schedule_reindex_enqueues_backfill() -> None:
    with patch.object(reindex, "enqueue", AsyncMock()) as enq:
        await reindex.schedule_reindex("wrk_1")
    enq.assert_awaited_once_with("brain_index_backfill", "wrk_1")


# ── synthesizer superseded-history rendering ─────────────────────────────────

async def test_synthesizer_labels_history_superseded() -> None:
    from app.modules.brain import synthesizer
    captured = {}

    async def _fake_sonnet(system, user, *, stage, **kw):
        captured["user"] = user
        return ({"answer": "x", "grounded": True, "usedSkillIds": ["skl_1"],
                 "confidence": 0.8}, StageUsage("s", "m", 1, 1, 0.0))

    with patch.object(synthesizer, "sonnet_json", _fake_sonnet):
        await synthesizer.answer(
            "what did the policy used to be",
            [{"id": "skl_1", "name": "Refund", "base_logic": "45 days"}],
            top_similarity=0.9,
            history=[{"skill_id": "skl_1", "version": "v1", "content": "30 days"}],
        )
    assert "SUPERSEDED" in captured["user"] and "30 days" in captured["user"]
