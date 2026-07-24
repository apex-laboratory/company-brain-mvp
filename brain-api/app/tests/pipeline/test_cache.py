"""Read-cache key derivation (``app/pipeline/cache.py``)."""
from __future__ import annotations

from app.pipeline.cache import search_key


def test_default_kind_is_query_and_backwards_compatible() -> None:
    # The skills surface calls without ``kind``; its historical key must be unchanged.
    assert search_key("ws1", "how do we refund") == search_key(
        "ws1", "how do we refund", kind="query"
    )
    assert search_key("ws1", "how do we refund").startswith("skills:ws1:query:")


def test_distinct_kinds_do_not_collide() -> None:
    # brain-chat and the skills surface cache different contracts under the same
    # (workspace, question) — a distinct kind must keep their keys apart.
    skills = search_key("ws1", "how do we refund", kind="query")
    brain = search_key("ws1", "how do we refund", kind="brain")
    assert skills != brain


def test_all_kinds_stay_on_invalidated_keyspace() -> None:
    # invalidate_skills clears ``skills:{ws}:*`` — every facet must live under it.
    for kind in ("query", "brain"):
        assert search_key("ws9", "q", kind=kind).startswith("skills:ws9:")


def test_query_is_case_and_whitespace_normalized() -> None:
    assert search_key("ws1", "  Refund Policy ") == search_key("ws1", "refund policy")
