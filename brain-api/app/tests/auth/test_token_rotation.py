"""Unit tests for refresh-token rotation and logout (KAN-50).

These exercise the service against an in-memory fake repository, so they run
without a database. Cross-tenant / RLS behaviour is covered by integration
tests; here we assert the rotation state machine, reuse detection, and the
idempotent logout contract.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from jose import jwt
from pydantic import ValidationError
from starlette.requests import Request

from app.config.settings import settings
from app.modules.auth.repository import AuthRepository, MembershipRecord, RefreshTokenRow
from app.modules.auth.router import _refresh_failed, _token_from
from app.modules.auth.schemas import LogoutRequest, RefreshRequest
from app.modules.auth.service import AuthService
from app.shared.errors.app_error import UnauthorizedError
from app.shared.helpers.crypto import sha256_hash


# ── fakes ─────────────────────────────────────────────────────────────────────
class FakeSession:
    """Records commits; the fake repo does the bookkeeping."""

    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


@dataclass
class _Row:
    id: str
    user_id: str
    token_hash: bytes
    family_id: str
    expires_at: datetime
    revoked_at: datetime | None = None
    replaced_by: str | None = None


@dataclass
class FakeRepository(AuthRepository):
    rows: list[_Row] = field(default_factory=list)
    membership: MembershipRecord | None = None
    revoke_family_calls: list[str] = field(default_factory=list)
    for_update_flags: list[bool] = field(default_factory=list)
    _counter: int = 0

    # -- reads --
    async def find_by_token_hash(
        self, session: Any, token_hash: bytes, *, for_update: bool = False
    ) -> RefreshTokenRow | None:
        self.for_update_flags.append(for_update)
        for row in self.rows:
            if row.token_hash == token_hash:
                return RefreshTokenRow(
                    id=row.id,
                    user_id=row.user_id,
                    family_id=row.family_id,
                    expires_at=row.expires_at,
                    revoked_at=row.revoked_at,
                )
        return None

    async def find_primary_membership(
        self, session: Any, user_id: str
    ) -> MembershipRecord | None:
        return self.membership

    # -- writes --
    async def insert_refresh_token(
        self,
        session: Any,
        *,
        user_id: str,
        token_hash: bytes,
        family_id: str,
        expires_at: datetime,
        user_agent: str | None,
        ip_address: str | None,
    ) -> str:
        self._counter += 1
        new_id = f"rt-uuid-{self._counter}"
        self.rows.append(
            _Row(
                id=new_id,
                user_id=user_id,
                token_hash=token_hash,
                family_id=family_id,
                expires_at=expires_at,
            )
        )
        return new_id

    async def revoke_token(
        self, session: Any, *, token_id: str, replaced_by: str | None = None
    ) -> None:
        for row in self.rows:
            if row.id == token_id and row.revoked_at is None:
                row.revoked_at = datetime.now(UTC)
                row.replaced_by = replaced_by

    async def revoke_family(self, session: Any, family_id: str) -> None:
        self.revoke_family_calls.append(family_id)
        for row in self.rows:
            if row.family_id == family_id and row.revoked_at is None:
                row.revoked_at = datetime.now(UTC)


# ── helpers ───────────────────────────────────────────────────────────────────
def _future() -> datetime:
    return datetime.now(UTC) + timedelta(days=30)


def _seed(repo: FakeRepository, raw: str, **overrides: Any) -> _Row:
    row = _Row(
        id=overrides.get("id", "rt-old"),
        user_id=overrides.get("user_id", "usr_1"),
        token_hash=sha256_hash(raw),
        family_id=overrides.get("family_id", "rt_fam"),
        expires_at=overrides.get("expires_at", _future()),
        revoked_at=overrides.get("revoked_at"),
    )
    repo.rows.append(row)
    return row


def _decode(token: str) -> dict[str, Any]:
    claims: dict[str, Any] = jwt.decode(token, settings.jwt_access_secret, algorithms=["HS256"])
    return claims


# ── refresh: happy path ───────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_refresh_rotates_and_returns_new_pair() -> None:
    repo = FakeRepository(
        membership=MembershipRecord(
            workspace_id="wrk_1", role="admin",
            workspace_name="Riverline", workspace_slug="riverline",
        )
    )
    old = _seed(repo, "raw-old")
    service = AuthService(repository=repo)
    session = FakeSession()

    issued = await service.refresh(session, raw_token="raw-old")  # type: ignore[arg-type]

    # New access token carries the user's current membership claims.
    claims = _decode(issued.access_token)
    assert claims["sub"] == "usr_1"
    assert claims["workspace_id"] == "wrk_1"
    assert claims["role"] == "admin"
    assert "exp" in claims

    # A genuinely new refresh secret was issued.
    assert issued.refresh_token != "raw-old"

    # Old row is revoked and linked to the new row (same family).
    assert old.revoked_at is not None
    assert old.replaced_by is not None
    new_rows = [r for r in repo.rows if r.id != old.id]
    assert len(new_rows) == 1
    assert new_rows[0].family_id == "rt_fam"
    assert new_rows[0].revoked_at is None
    assert old.replaced_by == new_rows[0].id
    assert session.commits == 1
    # The presented token is looked up under a row lock so concurrent rotations
    # serialize rather than forking the family.
    assert repo.for_update_flags == [True]


@pytest.mark.asyncio
async def test_refresh_pre_onboarding_user_has_null_workspace_claims() -> None:
    repo = FakeRepository(membership=None)  # no workspace yet
    _seed(repo, "raw-old")
    service = AuthService(repository=repo)

    issued = await service.refresh(FakeSession(), raw_token="raw-old")  # type: ignore[arg-type]

    claims = _decode(issued.access_token)
    assert claims["workspace_id"] is None
    assert claims["role"] is None


# ── refresh: failure paths ────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_refresh_unknown_token_raises_401() -> None:
    service = AuthService(repository=FakeRepository())
    with pytest.raises(UnauthorizedError) as exc:
        await service.refresh(FakeSession(), raw_token="nope")  # type: ignore[arg-type]
    assert exc.value.status == 401
    assert exc.value.message == "Invalid refresh token"


@pytest.mark.asyncio
async def test_refresh_reuse_revokes_entire_family_and_raises_401() -> None:
    repo = FakeRepository(
        membership=MembershipRecord(
            workspace_id="wrk_1", role="admin",
            workspace_name="W", workspace_slug="w",
        )
    )
    # Two live siblings plus the already-revoked token being replayed.
    _seed(repo, "raw-sibling", id="rt-sib", family_id="rt_fam")
    _seed(repo, "raw-revoked", id="rt-rev", family_id="rt_fam", revoked_at=datetime.now(UTC))
    service = AuthService(repository=repo)
    session = FakeSession()

    with pytest.raises(UnauthorizedError) as exc:
        await service.refresh(session, raw_token="raw-revoked")  # type: ignore[arg-type]

    assert exc.value.message == "Session invalidated due to token reuse. Please sign in again."
    assert repo.revoke_family_calls == ["rt_fam"]
    # The live sibling is now revoked too; no replacement token was minted.
    assert all(r.revoked_at is not None for r in repo.rows)
    assert len(repo.rows) == 2
    assert session.commits == 1


@pytest.mark.asyncio
async def test_refresh_expired_token_raises_401_without_rotation() -> None:
    repo = FakeRepository(
        membership=MembershipRecord(
            workspace_id="wrk_1", role="admin",
            workspace_name="W", workspace_slug="w",
        )
    )
    past = datetime.now(UTC) - timedelta(seconds=1)
    _seed(repo, "raw-old", expires_at=past)
    service = AuthService(repository=repo)

    with pytest.raises(UnauthorizedError) as exc:
        await service.refresh(FakeSession(), raw_token="raw-old")  # type: ignore[arg-type]

    assert exc.value.message == "Refresh token expired"
    assert len(repo.rows) == 1  # nothing minted


@pytest.mark.asyncio
async def test_refresh_reuse_logs_security_warning_without_raw_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[str, dict[str, Any]]] = []

    class _RecordingLogger:
        def warning(self, event: str, **kwargs: Any) -> None:
            captured.append((event, kwargs))

    from app.modules.auth import service as service_module

    monkeypatch.setattr(service_module, "log", _RecordingLogger())

    repo = FakeRepository()
    _seed(repo, "raw-revoked", revoked_at=datetime.now(UTC))
    service = AuthService(repository=repo)

    with pytest.raises(UnauthorizedError):
        await service.refresh(FakeSession(), raw_token="raw-revoked")  # type: ignore[arg-type]

    assert len(captured) == 1
    event, kwargs = captured[0]
    assert event == "refresh_token_reuse_detected"
    assert kwargs["user_id"] == "usr_1"
    assert kwargs["family_id"] == "rt_fam"
    # Never log the raw token (by name or value).
    assert "refresh_token" not in kwargs
    assert "raw-revoked" not in kwargs.values()


# ── logout ────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_logout_revokes_token() -> None:
    repo = FakeRepository()
    row = _seed(repo, "raw-old")
    service = AuthService(repository=repo)
    session = FakeSession()

    await service.logout(session, raw_token="raw-old")  # type: ignore[arg-type]

    assert row.revoked_at is not None
    assert row.replaced_by is None  # logout does not link a replacement
    assert session.commits == 1


@pytest.mark.asyncio
async def test_logout_unknown_token_is_idempotent() -> None:
    service = AuthService(repository=FakeRepository())
    session = FakeSession()

    await service.logout(session, raw_token="never-issued")  # type: ignore[arg-type]

    assert session.commits == 1  # still succeeds quietly


@pytest.mark.asyncio
async def test_logout_already_revoked_token_does_not_double_revoke() -> None:
    repo = FakeRepository()
    first = datetime.now(UTC) - timedelta(minutes=5)
    row = _seed(repo, "raw-old", revoked_at=first)
    service = AuthService(repository=repo)

    await service.logout(FakeSession(), raw_token="raw-old")  # type: ignore[arg-type]

    assert row.revoked_at == first  # unchanged


# ── request schema contract ───────────────────────────────────────────────────
@pytest.mark.parametrize("model", [RefreshRequest, LogoutRequest])
def test_request_accepts_camelcase_body(
    model: type[RefreshRequest] | type[LogoutRequest],
) -> None:
    parsed = model.model_validate({"refreshToken": "eyJ..."})
    assert parsed.refresh_token == "eyJ..."


@pytest.mark.parametrize("model", [RefreshRequest, LogoutRequest])
def test_request_rejects_unknown_keys(
    model: type[RefreshRequest] | type[LogoutRequest],
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate({"refreshToken": "eyJ...", "evil": "x"})


# ── router token resolution + failure handling ────────────────────────────────
def _request(cookie: str | None = None) -> Request:
    headers = [(b"cookie", cookie.encode())] if cookie else []
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/auth/refresh",
            "headers": headers,
            "state": {"request_id": "req_1"},
        }
    )


def test_token_from_prefers_body_over_cookie() -> None:
    body = RefreshRequest(refresh_token="from-body")
    assert _token_from(body, _request("refresh_token=from-cookie")) == "from-body"


def test_token_from_falls_back_to_cookie_when_body_empty() -> None:
    # The httpOnly-cookie flow sends no body; the token must still be found.
    assert _token_from(None, _request("refresh_token=from-cookie")) == "from-cookie"
    assert _token_from(RefreshRequest(), _request("refresh_token=from-cookie")) == "from-cookie"


def test_token_from_returns_none_without_body_or_cookie() -> None:
    assert _token_from(None, _request()) is None


def test_refresh_failed_returns_401_and_clears_cookie() -> None:
    response = _refresh_failed(_request(), UnauthorizedError("Invalid refresh token"))

    assert response.status_code == 401
    set_cookies = [v.decode() for k, v in response.raw_headers if k == b"set-cookie"]
    joined = " ".join(set_cookies).lower()
    assert "refresh_token=" in joined
    assert "max-age=0" in joined  # cookie deletion
