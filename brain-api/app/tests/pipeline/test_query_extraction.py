"""Query-driven extraction fallback tests (Phase 5 — Feature 16 contract).

Exercises the three guaranteed outcomes: a genuine no-match, the budget-exceeded
async deferral, and the found-candidates deferral — without needing live sources.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from app.pipeline import query_extraction as qe
from app.shared.middleware.authenticate import AuthContext


def _auth() -> AuthContext:
    return AuthContext(user_id="usr_1", workspace_id="wrk_1", role="viewer",
                       scopes=["brain:query"], kind="api_key")


async def test_no_candidates_is_real_no_match() -> None:
    # search_sources returns [] by default (no provider search implemented).
    result = await qe.run_query_extraction(
        _auth(), "refund past 30 days", prior={"match_type": "no_match", "interaction_id": "int_1"}
    )
    assert result["match_type"] == "no_match"
    assert result["extraction_queued"] is False
    assert result["interaction_id"] == "int_1"  # preserved across the fallback


async def test_budget_exceeded_defers_to_async() -> None:
    async def _slow(_auth, _situation):  # exceed the budget
        await asyncio.sleep(0.2)
        return []

    with patch.object(qe, "BUDGET_SECONDS", 0.01), \
         patch.object(qe, "search_sources", _slow), \
         patch.object(qe, "_enqueue_async", AsyncMock()) as enq:
        result = await qe.run_query_extraction(_auth(), "refund", prior={"interaction_id": "int_1"})
    assert result["extraction_queued"] is True
    assert result["retry_after_seconds"] == qe.RETRY_AFTER_SECONDS
    enq.assert_awaited_once()


async def test_found_candidates_queue_async() -> None:
    with patch.object(
        qe, "search_sources",
        AsyncMock(return_value=[{"provider": "zendesk", "content": "policy text"}]),
    ), patch.object(qe, "_enqueue_async", AsyncMock()) as enq:
        result = await qe.run_query_extraction(_auth(), "refund", prior={})
    assert result["extraction_queued"] is True
    enq.assert_awaited_once()
