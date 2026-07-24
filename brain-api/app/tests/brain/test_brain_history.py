"""Brain conversation history read-back tests.

The dashboard persists every turn (``brain_conversations``/``brain_messages``); these
endpoints replay it on reload. Asserts: conversations list newest-active first for
the JWT owner; a thread's turns replay oldest-first with stored sources mapped back to
the live ``SourceCitation`` shape; an unknown/unowned thread 404s; and both surfaces
are dashboard-only — an agent (API-key) is a 403, never a misleading empty list.
"""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules.brain import service as service_module
from app.modules.brain.service import BrainService
from app.shared.errors.app_error import ForbiddenError, NotFoundError
from app.shared.middleware.authenticate import AuthContext

_NOW = datetime(2026, 7, 12, 9, 0, tzinfo=UTC)


class _AsyncCtx:
    def __init__(self, value: object) -> None:
        self._value = value

    async def __aenter__(self) -> object:
        return self._value

    async def __aexit__(self, *_: object) -> bool:
        return False


def _auth(kind: str = "jwt") -> AuthContext:
    return AuthContext(
        user_id="usr_1", workspace_id="wrk_1", role="viewer",
        scopes=["brain:query"], kind=kind,  # type: ignore[arg-type]
    )


def _svc(repo: MagicMock) -> tuple[BrainService, tuple]:
    svc = BrainService(repository=repo, skills=MagicMock(), pipeline=MagicMock())
    patches = (
        patch.object(service_module, "get_tenant_session", return_value=_AsyncCtx(MagicMock())),
        patch.object(service_module, "run_in_tenant", return_value=_AsyncCtx(None)),
    )
    return svc, patches


def _enter(patches: tuple) -> None:
    for p in patches:
        p.start()


def _exit(patches: tuple) -> None:
    for p in patches:
        p.stop()


@pytest.mark.asyncio
async def test_list_conversations_maps_rows_for_jwt_owner() -> None:
    repo = MagicMock(
        list_conversations=AsyncMock(
            return_value=[
                {"id": "cnv_1", "title": "Refunds", "created_at": _NOW, "updated_at": _NOW},
            ]
        )
    )
    svc, patches = _svc(repo)
    _enter(patches)
    try:
        result = await svc.list_conversations(_auth(), limit=50)
    finally:
        _exit(patches)
    repo.list_conversations.assert_awaited_once()
    assert repo.list_conversations.await_args.kwargs["limit"] == 50
    assert [c.id for c in result] == ["cnv_1"]
    assert result[0].title == "Refunds"


@pytest.mark.asyncio
async def test_list_conversations_rejects_api_key() -> None:
    repo = MagicMock(list_conversations=AsyncMock())
    svc, patches = _svc(repo)
    _enter(patches)
    try:
        with pytest.raises(ForbiddenError):
            await svc.list_conversations(_auth(kind="api_key"), limit=50)
    finally:
        _exit(patches)
    repo.list_conversations.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_messages_replays_and_maps_stored_sources() -> None:
    repo = MagicMock(
        conversation_exists=AsyncMock(return_value=True),
        list_messages=AsyncMock(
            return_value=[
                {"id": "msg_1", "role": "user", "content": "refund window?",
                 "confidence": None, "sources": None, "created_at": _NOW},
                {"id": "msg_2", "role": "assistant", "content": "45 days.",
                 "confidence": 82, "created_at": _NOW,
                 "sources": [{"provider": "notion", "label": "Refund Policy",
                              "sourceItemId": "skl_1",
                              "url": "https://n/1", "excerpt": "…45 days…"}]},
            ]
        ),
    )
    svc, patches = _svc(repo)
    _enter(patches)
    try:
        result = await svc.list_messages(_auth(), "cnv_1")
    finally:
        _exit(patches)
    assert [m.id for m in result] == ["msg_1", "msg_2"]
    assert result[0].sources == []
    # stored {label, sourceItemId} → live {location, skillId}
    src = result[1].sources[0]
    assert src.location == "Refund Policy"
    assert src.skill_id == "skl_1"
    assert src.provider == "notion"


@pytest.mark.asyncio
async def test_list_messages_unknown_conversation_404s() -> None:
    repo = MagicMock(
        conversation_exists=AsyncMock(return_value=False),
        list_messages=AsyncMock(),
    )
    svc, patches = _svc(repo)
    _enter(patches)
    try:
        with pytest.raises(NotFoundError):
            await svc.list_messages(_auth(), "cnv_missing")
    finally:
        _exit(patches)
    repo.list_messages.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_messages_rejects_api_key() -> None:
    repo = MagicMock(conversation_exists=AsyncMock(), list_messages=AsyncMock())
    svc, patches = _svc(repo)
    _enter(patches)
    try:
        with pytest.raises(ForbiddenError):
            await svc.list_messages(_auth(kind="api_key"), "cnv_1")
    finally:
        _exit(patches)
    repo.conversation_exists.assert_not_awaited()
