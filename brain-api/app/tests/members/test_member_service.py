"""Service unit tests for the members module.

Drives ``MemberService`` against a fake repository with the DB session, tenant
context, privileged session, and Resend send all stubbed. Asserts the roster maps
the current-user flag and seat meta (active + pending), that inviting stores only
the token *hash* (never the raw token) and sends the acceptance email, and that
the duplicate / seat-limit / missing-workspace / non-member guards each raise.
"""
from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest

from app.modules.members import service as service_module
from app.modules.members.repository import EmailStatus, MemberRepository, MemberRow
from app.modules.members.schemas import MemberInviteRequest
from app.modules.members.service import MemberService
from app.shared.errors.app_error import ConflictError, ForbiddenError, NotFoundError
from app.shared.helpers.crypto import sha256_hash
from app.shared.middleware.authenticate import AuthContext

_NOW = datetime(2026, 6, 4, 10, 0, tzinfo=UTC)


def _auth(workspace_id: str | None = "wrk_1", role: str = "admin") -> AuthContext:
    return AuthContext(
        user_id="usr_1", workspace_id=workspace_id, role=role, scopes=[], kind="jwt"
    )


class _FakeRepo(MemberRepository):
    def __init__(
        self,
        *,
        members: list[MemberRow] | None = None,
        seat_limit: int | None = 5,
        active_members: int = 1,
        pending_invites: int = 0,
        status: EmailStatus | None = None,
    ) -> None:
        self._members = members or []
        self._seat_limit = seat_limit
        self._active_members = active_members
        self._pending_invites = pending_invites
        self._status = status or EmailStatus(is_member=False, has_pending_invite=False)
        self.created: dict[str, Any] | None = None

    async def list_members(self, session: Any, workspace_id: str) -> list[MemberRow]:
        return self._members

    async def get_seat_limit(self, session: Any, workspace_id: str) -> int | None:
        return self._seat_limit

    async def count_active_members(self, session: Any, workspace_id: str) -> int:
        return self._active_members

    async def count_pending_invites(self, session: Any, workspace_id: str) -> int:
        return self._pending_invites

    async def email_status(
        self, session: Any, workspace_id: str, email: str
    ) -> EmailStatus:
        return self._status

    async def create_invitation(self, session: Any, **kwargs: Any) -> datetime:
        self.created = kwargs
        return _NOW


class _FakeSession:
    async def commit(self) -> None:  # pragma: no cover - trivial
        pass

    async def rollback(self) -> None:  # pragma: no cover - trivial
        pass


@pytest.fixture(autouse=True)
def _stub_io(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Stub the tenant session, privileged session, and Resend send.

    Returns the list that captures any email-send calls so tests can assert on
    them without hitting the network.
    """

    @contextlib.asynccontextmanager
    async def fake_session(*_args: Any, **_kwargs: Any) -> AsyncIterator[_FakeSession]:
        yield _FakeSession()

    monkeypatch.setattr(service_module, "tenant_session", fake_session)
    monkeypatch.setattr(service_module, "get_session", fake_session)

    sent: list[dict[str, Any]] = []

    async def fake_send_email(**kwargs: Any) -> str:
        sent.append(kwargs)
        return "msg_test"

    monkeypatch.setattr(service_module.resend, "send_email", fake_send_email)
    return sent


@pytest.mark.asyncio
async def test_list_flags_current_user_and_counts_pending_in_seats() -> None:
    repo = _FakeRepo(
        members=[
            MemberRow(
                id="usr_1",
                name="Dana Reyes",
                email="dana@riverline.io",
                role="admin",
                title="Head of CX",
                avatar_color="#C2603A",
            ),
            MemberRow(
                id="usr_2",
                name="Sam Lee",
                email="sam@riverline.io",
                role="viewer",
                title=None,
                avatar_color=None,
            ),
        ],
        seat_limit=12,
        pending_invites=1,
    )
    service = MemberService(repository=repo)

    roster = await service.list_members(_auth(), "wrk_1")

    assert roster.seat_limit == 12
    assert roster.pending_invites == 1
    # used_seats = active members (2) + pending invites (1) — matches enforcement.
    assert roster.used_seats == 3
    assert [m.id for m in roster.members] == ["usr_1", "usr_2"]
    assert roster.members[0].is_current_user is True
    assert roster.members[1].is_current_user is False
    dumped = roster.members[0].model_dump(by_alias=True)
    assert dumped["avatarColor"] == "#C2603A"
    assert dumped["isCurrentUser"] is True


@pytest.mark.asyncio
async def test_list_missing_workspace_maps_to_404() -> None:
    service = MemberService(repository=_FakeRepo(seat_limit=None))

    with pytest.raises(NotFoundError):
        await service.list_members(_auth(), "wrk_1")


@pytest.mark.asyncio
async def test_list_non_member_forbidden() -> None:
    service = MemberService(repository=_FakeRepo())

    with pytest.raises(ForbiddenError):
        await service.list_members(_auth(workspace_id="wrk_OTHER"), "wrk_1")


@pytest.mark.asyncio
async def test_invite_stores_hash_sends_email_and_returns_pending(
    _stub_io: list[dict[str, Any]],
) -> None:
    repo = _FakeRepo(seat_limit=5, active_members=1, pending_invites=0)
    service = MemberService(repository=repo)
    body = MemberInviteRequest(email="New.Person@Riverline.io", role="editor")

    result = await service.invite_member(_auth(), "wrk_1", body)

    assert result.invite_id.startswith("inv_")
    assert result.status == "pending"
    assert result.role == "editor"
    assert result.email == "new.person@riverline.io"  # lowercased at the edge

    assert repo.created is not None
    assert repo.created["email"] == "new.person@riverline.io"
    assert repo.created["invited_by"] == "usr_1"
    token_hash = repo.created["token_hash"]
    assert isinstance(token_hash, bytes) and len(token_hash) == 32
    assert "token" not in repo.created
    assert token_hash != sha256_hash("")  # a real token was hashed

    # The acceptance email was sent to the invitee with an accept link.
    assert len(_stub_io) == 1
    sent = _stub_io[0]
    assert sent["to"] == "new.person@riverline.io"
    assert "/invite/accept?token=" in sent["html"]


@pytest.mark.asyncio
async def test_invite_email_failure_does_not_fail_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(**_kwargs: Any) -> str:
        raise service_module.resend.EmailError("down")

    monkeypatch.setattr(service_module.resend, "send_email", boom)

    repo = _FakeRepo(seat_limit=5, active_members=1, pending_invites=0)
    service = MemberService(repository=repo)
    body = MemberInviteRequest(email="x@riverline.io", role="viewer")

    # Best-effort: a delivery failure is swallowed, the invite still succeeds.
    result = await service.invite_member(_auth(), "wrk_1", body)
    assert result.status == "pending"
    assert repo.created is not None


@pytest.mark.asyncio
async def test_invite_existing_member_conflicts() -> None:
    repo = _FakeRepo(status=EmailStatus(is_member=True, has_pending_invite=False))
    service = MemberService(repository=repo)
    body = MemberInviteRequest(email="dana@riverline.io", role="viewer")

    with pytest.raises(ConflictError):
        await service.invite_member(_auth(), "wrk_1", body)
    assert repo.created is None


@pytest.mark.asyncio
async def test_invite_duplicate_pending_conflicts() -> None:
    repo = _FakeRepo(status=EmailStatus(is_member=False, has_pending_invite=True))
    service = MemberService(repository=repo)
    body = MemberInviteRequest(email="sam@riverline.io", role="viewer")

    with pytest.raises(ConflictError):
        await service.invite_member(_auth(), "wrk_1", body)
    assert repo.created is None


@pytest.mark.asyncio
async def test_invite_rejected_when_seats_full() -> None:
    # 4 active + 1 pending == seat_limit 5 → no seat left for another invite.
    repo = _FakeRepo(seat_limit=5, active_members=4, pending_invites=1)
    service = MemberService(repository=repo)
    body = MemberInviteRequest(email="extra@riverline.io", role="viewer")

    with pytest.raises(ConflictError):
        await service.invite_member(_auth(), "wrk_1", body)
    assert repo.created is None


@pytest.mark.asyncio
async def test_invite_missing_workspace_maps_to_404() -> None:
    service = MemberService(repository=_FakeRepo(seat_limit=None))
    body = MemberInviteRequest(email="x@riverline.io", role="viewer")

    with pytest.raises(NotFoundError):
        await service.invite_member(_auth(), "wrk_1", body)


@pytest.mark.asyncio
async def test_invite_non_member_forbidden() -> None:
    service = MemberService(repository=_FakeRepo())
    body = MemberInviteRequest(email="x@riverline.io", role="viewer")

    with pytest.raises(ForbiddenError):
        await service.invite_member(_auth(workspace_id="wrk_OTHER"), "wrk_1", body)
