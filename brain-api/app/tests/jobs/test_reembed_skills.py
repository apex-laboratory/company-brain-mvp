"""``reembed_skills`` job tests — the skills-table half of an embedding migration.

The job's whole reason to exist is that a vector is only comparable to vectors
from the same model, so these pin the properties that make a model rotation safe:
only stale rows are re-embedded, the new provenance is stamped, network I/O stays
out of the transactions, and one bad row can't abort the batch. All mocked — no
DB, no OpenAI.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.jobs.tasks import reembed_skills as job_module
from app.pipeline.types import StageUsage

_MODEL = "text-embedding-3-large"


class _AsyncCtx:
    def __init__(self, value: object) -> None:
        self._value = value

    async def __aenter__(self) -> object:
        return self._value

    async def __aexit__(self, *_: object) -> bool:
        return False


def _rows() -> list[dict]:
    return [
        {"id": "skl_1", "trigger": "customer wants refund", "base_logic": "refund in 30d"},
        {"id": "skl_2", "trigger": "vendor invoice", "base_logic": "pay net 60"},
    ]


def _repo_mock(rows: list[dict] | None = None) -> MagicMock:
    return MagicMock(
        list_stale_embeddings=AsyncMock(return_value=_rows() if rows is None else rows),
        set_embedding=AsyncMock(),
    )


def _patches(repo: MagicMock, session: MagicMock, embed: AsyncMock | None = None):
    return (
        patch.object(job_module, "_repo", repo),
        patch.object(job_module, "get_tenant_session", return_value=_AsyncCtx(session)),
        patch.object(job_module, "run_in_tenant", return_value=_AsyncCtx(None)),
        patch.object(job_module.embedder, "current_model", lambda: _MODEL),
        patch.object(
            job_module.embedder, "embed_text",
            embed or AsyncMock(return_value=([0.1] * 1536, StageUsage("e", _MODEL, 1, 0, 0.0))),
        ),
    )


async def _run(repo, session, embed=None, **kwargs) -> dict:
    patches = _patches(repo, session, embed)
    for p in patches:
        p.start()
    try:
        return await job_module.reembed_skills({}, "wrk_1", **kwargs)
    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_reembeds_stale_skills_and_stamps_provenance() -> None:
    repo = _repo_mock()
    session = MagicMock(commit=AsyncMock())
    result = await _run(repo, session)

    assert result == {
        "reembedded": 2, "failed": 0, "embeddingModel": _MODEL, "forced": False,
    }
    assert repo.set_embedding.await_count == 2
    # Every write carries the model that actually produced the vector — that stamp
    # is what makes the next run able to tell fresh from stale.
    for call in repo.set_embedding.await_args_list:
        assert call.kwargs["embedding_model"] == _MODEL
        assert len(call.kwargs["embedding"]) == 1536
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_selects_only_rows_not_on_the_configured_model() -> None:
    repo = _repo_mock()
    await _run(repo, MagicMock(commit=AsyncMock()))
    repo.list_stale_embeddings.assert_awaited_once()
    kwargs = repo.list_stale_embeddings.await_args.kwargs
    assert kwargs["embedding_model"] == _MODEL
    assert kwargs["force"] is False


@pytest.mark.asyncio
async def test_force_widens_the_selection() -> None:
    repo = _repo_mock()
    result = await _run(repo, MagicMock(commit=AsyncMock()), force=True)
    assert repo.list_stale_embeddings.await_args.kwargs["force"] is True
    assert result["forced"] is True


@pytest.mark.asyncio
async def test_nothing_stale_is_a_no_op() -> None:
    repo = _repo_mock(rows=[])
    session = MagicMock(commit=AsyncMock())
    embed = AsyncMock()
    result = await _run(repo, session, embed=embed)

    assert result == {
        "reembedded": 0, "failed": 0, "embeddingModel": _MODEL, "forced": False,
    }
    embed.assert_not_awaited()          # no embedding spend on a current corpus
    repo.set_embedding.assert_not_awaited()
    session.commit.assert_not_awaited()  # no write transaction opened at all


@pytest.mark.asyncio
async def test_embeds_the_canonical_trigger_plus_base_logic_text() -> None:
    repo = _repo_mock(rows=[_rows()[0]])
    embed = AsyncMock(return_value=([0.1] * 1536, StageUsage("e", _MODEL, 1, 0, 0.0)))
    await _run(repo, MagicMock(commit=AsyncMock()), embed=embed)
    # Must reproduce exactly what the write path embedded, or the refreshed vector
    # lands elsewhere in the space and the tuned thresholds stop meaning anything.
    assert embed.await_args.args[0] == "customer wants refund\nrefund in 30d"


@pytest.mark.asyncio
async def test_one_failed_row_does_not_abort_the_batch() -> None:
    repo = _repo_mock()
    session = MagicMock(commit=AsyncMock())
    embed = AsyncMock(
        side_effect=[
            RuntimeError("upstream 500"),
            ([0.1] * 1536, StageUsage("e", _MODEL, 1, 0, 0.0)),
        ]
    )
    result = await _run(repo, session, embed=embed)

    assert result["reembedded"] == 1
    assert result["failed"] == 1
    # The survivor is still written — a partial migration is resumable, an aborted
    # one throws away every embedding already paid for.
    repo.set_embedding.assert_awaited_once()
    assert repo.set_embedding.await_args.kwargs["embedding_model"] == _MODEL


@pytest.mark.asyncio
async def test_skill_with_no_text_is_skipped_not_embedded() -> None:
    repo = _repo_mock(rows=[{"id": "skl_3", "trigger": None, "base_logic": None}])
    embed = AsyncMock()
    result = await _run(repo, MagicMock(commit=AsyncMock()), embed=embed)
    embed.assert_not_awaited()
    assert result["reembedded"] == 0
    repo.set_embedding.assert_not_awaited()


@pytest.mark.asyncio
async def test_embedding_happens_outside_both_transactions() -> None:
    """Network I/O must never pin a pooled connection (BACKEND_BEST_PRACTICES §7).

    Asserted structurally: the read session is closed before ``embed_text`` runs,
    and the write session opens after — so the embed can't be holding either.
    """
    order: list[str] = []

    async def _read(*_a, **_k) -> list[dict]:
        order.append("read")
        return _rows()

    repo = MagicMock(
        list_stale_embeddings=AsyncMock(side_effect=_read),
        set_embedding=AsyncMock(side_effect=lambda *a, **k: order.append("write")),
    )
    embed = AsyncMock(
        side_effect=lambda *a, **k: (
            order.append("embed"), ([0.1] * 1536, StageUsage("e", _MODEL, 1, 0, 0.0))
        )[1]
    )
    await _run(repo, MagicMock(commit=AsyncMock()), embed=embed)
    assert order == ["read", "embed", "embed", "write", "write"]
