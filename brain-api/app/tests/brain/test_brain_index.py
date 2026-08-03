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
from app.pipeline import embedder
from app.pipeline.types import StageUsage

_MODEL = embedder.current_model()  # stamped on every chunk the backfill writes


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
         "url": "https://acme.slack.com/archives/C1/p123",
         "policy_title": "Refund policy escalation", "payload": None,  # slack: no native title
         "author": "U07A3B12",
         "content": "we need a 45-day refund window for premium customers"},
    ]


def _repo_mock(*, version_rows=None, evidence_rows=None, existing=None) -> MagicMock:
    return MagicMock(
        list_version_index_rows=AsyncMock(return_value=version_rows if version_rows is not None else _rows()),
        list_evidence_rows=AsyncMock(return_value=evidence_rows if evidence_rows is not None else []),
        list_fresh_chunk_keys=AsyncMock(return_value=existing if existing is not None else set()),
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
        # Default: no provider connection → author id kept verbatim (no network).
        patch.object(job_module, "token_for_provider", AsyncMock(return_value=None)),
        patch.object(job_module, "resolve_users", AsyncMock(return_value={})),
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
    assert result == {"indexed": 2, "versionChunks": 2, "evidenceChunks": 0,
                      "embeddingModel": _MODEL, "forced": False}
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
    assert result == {"indexed": 0, "versionChunks": 0, "evidenceChunks": 0,
                      "embeddingModel": _MODEL, "forced": False}
    repo.upsert_skill_version_chunk.assert_not_awaited()  # nothing new to write
    embed.assert_not_awaited()                            # no embedding spend on re-run
    repo.sync_skill_version_current.assert_awaited_once()  # is_current still reconciled


async def test_backfill_skips_only_chunks_fresh_for_the_current_model() -> None:
    """Freshness is asked for by model, not "does a row exist".

    That predicate is the whole migration story: a chunk embedded by a *different*
    model isn't returned as fresh, so it gets re-planned and its vector refreshed
    in place — a model rotation is this job re-run, not a manual truncate.
    """
    repo = _repo_mock()
    session = MagicMock(commit=AsyncMock())
    patches = _job_patches(repo, session)
    for p in patches:
        p.start()
    try:
        await job_module.brain_index_backfill({}, "wrk_1")
    finally:
        for p in patches:
            p.stop()
    repo.list_fresh_chunk_keys.assert_awaited_once()
    assert repo.list_fresh_chunk_keys.await_args.kwargs["embedding_model"] == _MODEL


async def test_backfill_force_reembeds_already_indexed_chunks() -> None:
    """``force`` re-embeds regardless of provenance — for a change to the chunking
    or embedded text, which the ``embedding_model`` stamp alone can't detect."""
    existing = {chunk_key("skill_version", "skl_1", "v2", 0),
                chunk_key("skill_version", "skl_1", "v1", 0)}
    repo = _repo_mock(existing=existing)
    session = MagicMock(commit=AsyncMock())
    embed = AsyncMock(return_value=([0.0] * 1536, StageUsage("e", _MODEL, 1, 0, 0.0)))
    with patch.object(job_module, "_repo", repo), \
         patch.object(job_module, "get_tenant_session", return_value=_AsyncCtx(session)), \
         patch.object(job_module, "run_in_tenant", return_value=_AsyncCtx(None)), \
         patch.object(job_module.embedder, "embed_text", embed):
        result = await job_module.brain_index_backfill({}, "wrk_1", force=True)

    assert result["indexed"] == 2 and result["forced"] is True
    # Not even consulted — force means "nothing counts as fresh".
    repo.list_fresh_chunk_keys.assert_not_awaited()
    assert embed.await_count == 2
    assert repo.upsert_skill_version_chunk.await_count == 2


async def test_backfill_stamps_the_model_on_every_chunk() -> None:
    repo = _repo_mock(evidence_rows=_evidence_rows())
    session = MagicMock(commit=AsyncMock())
    patches = _job_patches(repo, session)
    for p in patches:
        p.start()
    try:
        await job_module.brain_index_backfill({}, "wrk_1")
    finally:
        for p in patches:
            p.stop()
    written = (
        repo.upsert_skill_version_chunk.await_args_list
        + repo.upsert_evidence_chunk.await_args_list
    )
    assert written  # guard: the assertion below is vacuous on an empty plan
    for call in written:
        assert call.kwargs["embedding_model"] == "m"  # the stub usage's model


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
    assert result == {"indexed": 1, "versionChunks": 0, "evidenceChunks": 1,
                      "embeddingModel": _MODEL, "forced": False}
    repo.upsert_evidence_chunk.assert_awaited_once()
    kw = repo.upsert_evidence_chunk.await_args.kwargs
    assert kw["skill_id"] == "skl_1"
    assert kw["source_ref"] == {
        "provider": "slack", "sourceItemId": "rev_1",
        "url": "https://acme.slack.com/archives/C1/p123",  # the source document link
        "label": "Refund policy escalation",  # slack has no native title → policy title
        "author": "U07A3B12",  # no connection to resolve against → raw id kept
    }
    assert kw["chunk_key"] == chunk_key("evidence", "skl_1", "rev_1", 0)


async def test_backfill_captures_notion_document_link_and_native_title() -> None:
    # A Notion-sourced policy: no message author, but the page link + its OWN title
    # are kept, so "which document made the policy update" is answerable (user ask).
    rows = [{
        "skill_id": "skl_1", "review_id": "rev_9", "provider": "notion",
        "url": "https://www.notion.so/Refund-Policy-abc123", "policy_title": "Refund rule",
        "payload": {"properties": {"Name": {"type": "title",
                                            "title": [{"plain_text": "Refund Policy 2026"}]}}},
        "author": None, "content": "premium customers get a 45-day refund window",
    }]
    repo = _repo_mock(version_rows=[], evidence_rows=rows)
    session = MagicMock(commit=AsyncMock())
    patches = _job_patches(repo, session)
    for p in patches:
        p.start()
    try:
        await job_module.brain_index_backfill({}, "wrk_1")
    finally:
        for p in patches:
            p.stop()
    ref = repo.upsert_evidence_chunk.await_args.kwargs["source_ref"]
    assert ref["provider"] == "notion"
    assert ref["url"] == "https://www.notion.so/Refund-Policy-abc123"  # clickable page
    assert ref["label"] == "Refund Policy 2026"  # the Notion page's own title, not the policy
    assert ref["author"] is None


async def test_backfill_resolves_evidence_author_name() -> None:
    repo = _repo_mock(version_rows=[], evidence_rows=_evidence_rows())
    session = MagicMock(commit=AsyncMock())
    with patch.object(job_module, "_repo", repo), \
         patch.object(job_module, "get_tenant_session", return_value=_AsyncCtx(session)), \
         patch.object(job_module, "run_in_tenant", return_value=_AsyncCtx(None)), \
         patch.object(job_module.embedder, "embed_text",
                      AsyncMock(return_value=([0.0] * 1536, StageUsage("e", "m", 1, 0, 0.0)))), \
         patch.object(job_module, "token_for_provider",
                      AsyncMock(return_value=("slack-token", None))) as tok, \
         patch.object(job_module, "resolve_users",
                      AsyncMock(return_value={"U07A3B12": "Jane Doe"})) as resolve:
        await job_module.brain_index_backfill({}, "wrk_1")
    # the Slack id was resolved to a name and stored on the evidence citation
    assert repo.upsert_evidence_chunk.await_args.kwargs["source_ref"]["author"] == "Jane Doe"
    tok.assert_awaited_once_with("wrk_1", "slack")
    assert resolve.await_args.args[0] == "slack" and "U07A3B12" in resolve.await_args.args[2]


async def test_backfill_evidence_is_idempotent() -> None:
    existing = {chunk_key("evidence", "skl_1", "rev_1", 0)}
    repo = _repo_mock(version_rows=[], evidence_rows=_evidence_rows(), existing=existing)
    session = MagicMock(commit=AsyncMock())
    embed = AsyncMock(return_value=([0.0] * 1536, StageUsage("e", "m", 1, 0, 0.0)))
    with patch.object(job_module, "_repo", repo), \
         patch.object(job_module, "get_tenant_session", return_value=_AsyncCtx(session)), \
         patch.object(job_module, "run_in_tenant", return_value=_AsyncCtx(None)), \
         patch.object(job_module.embedder, "embed_text", embed), \
         patch.object(job_module, "token_for_provider", AsyncMock(return_value=None)), \
         patch.object(job_module, "resolve_users", AsyncMock(return_value={})):
        result = await job_module.brain_index_backfill({}, "wrk_1")
    assert result["evidenceChunks"] == 0
    repo.upsert_evidence_chunk.assert_not_awaited()
    embed.assert_not_awaited()


# ── author resolution (decision F) ───────────────────────────────────────────

async def test_resolve_users_slack_maps_id_to_name() -> None:
    from app.pipeline.expanders import user_directory as ud

    class _Resp:
        def raise_for_status(self): ...
        def json(self):
            return {"ok": True, "user": {"real_name": "Jane Doe", "name": "jane"}}

    class _Client:
        async def get(self, url, **kw): return _Resp()

    with patch.object(ud, "http_client", lambda: _Client()):
        out = await ud.resolve_users("slack", "tok", ["U07A3B12", "U07A3B12"])
    assert out == {"U07A3B12": "Jane Doe"}  # deduped, resolved


async def test_resolve_users_zendesk_bulk() -> None:
    from app.pipeline.expanders import user_directory as ud

    class _Resp:
        def raise_for_status(self): ...
        def json(self):
            return {"users": [{"id": 380288, "name": "Bob R."}]}

    class _Client:
        async def get(self, url, **kw): return _Resp()

    with patch.object(ud, "http_client", lambda: _Client()):
        out = await ud.resolve_users("zendesk", "tok", ["380288"], subdomain="acme")
    assert out == {"380288": "Bob R."}


async def test_resolve_users_best_effort_never_raises() -> None:
    from app.pipeline.expanders import user_directory as ud

    class _Client:
        async def get(self, url, **kw): raise RuntimeError("boom")

    with patch.object(ud, "http_client", lambda: _Client()):
        # a lookup outage returns {} rather than propagating — the id is kept upstream
        assert await ud.resolve_users("zendesk", "tok", ["1"], subdomain="acme") == {}


async def test_resolve_users_unknown_provider_and_empty() -> None:
    from app.pipeline.expanders import user_directory as ud
    assert await ud.resolve_users("github", "tok", ["janedoe"]) == {}  # no lookup needed
    assert await ud.resolve_users("slack", "", ["U1"]) == {}           # no token
    assert await ud.resolve_users("slack", "tok", []) == {}            # no ids


# ── native document titles (per provider) ────────────────────────────────────

def test_document_title_per_provider() -> None:
    from app.pipeline.expanders.source_document import document_title
    assert document_title("jira", {"key": "PROJ-12", "fields": {"summary": "Refund SLA"}}) \
        == "PROJ-12: Refund SLA"
    assert document_title("github", {"issue": {"title": "Fix refund window"}}) \
        == "Fix refund window"
    assert document_title("github", {"pull_request": {"title": "Refund PR"}}) == "Refund PR"
    assert document_title("google_drive", {"name": "Refund Policy.gdoc"}) == "Refund Policy.gdoc"
    assert document_title("zendesk", {"subject": "Refund escalation"}) == "Refund escalation"
    assert document_title(
        "gmail", {"payload": {"headers": [{"name": "Subject", "value": "Refund change"}]}}
    ) == "Refund change"
    assert document_title(
        "notion", {"properties": {"P": {"type": "title", "title": [{"plain_text": "Refund"}]}}}
    ) == "Refund"


def test_document_title_none_for_slack_and_missing() -> None:
    from app.pipeline.expanders.source_document import document_title
    assert document_title("slack", {"text": "hi"}) is None   # a message has no title
    assert document_title("jira", None) is None              # nothing captured
    assert document_title("github", {"action": "opened"}) is None  # no title anywhere


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

    with patch.object(synthesizer, "gemini_json", _fake_sonnet):
        await synthesizer.answer(
            "what did the policy used to be",
            [{"id": "skl_1", "name": "Refund", "base_logic": "45 days"}],
            top_similarity=0.9,
            history=[{"skill_id": "skl_1", "version": "v1", "content": "30 days"}],
        )
    assert "SUPERSEDED" in captured["user"] and "30 days" in captured["user"]
