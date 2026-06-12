"""Tests for the passwordless email auth endpoints (KAN-49).

Two layers:
  * Service unit tests — drive ``AuthService`` against a fake repository and a
    fake DB session (no Postgres), asserting business rules and token claims.
  * Router contract tests — drive the FastAPI app through an ASGI transport with
    the service stubbed, asserting the response envelope, status codes, camelCase
    fields, and validation/conflict/not-found mapping.
"""
from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any, Literal

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from jose import jwt

from app.config.settings import settings
from app.modules.auth import service as service_module
from app.modules.auth.repository import AuthRepository, MembershipRecord, UserRecord
from app.modules.auth.schemas import (
    AuthSessionOut,
    EmailSignupRequest,
    UserOut,
    WorkspaceOut,
)
from app.modules.auth.service import AuthService
from app.shared.errors.app_error import ConflictError, NotFoundError
from app.shared.helpers.crypto import sha256_hash


# ── fakes ───────────────────────────────────────────────────────────────────────
class _FakeSession:
    def __init__(self) -> None:
        self.committed = False

    async def commit(self) -> None:
        self.committed = True


class _FakeAuthRepository(AuthRepository):
    """In-memory stand-in for AuthRepository capturing the service's writes."""

    def __init__(
        self,
        *,
        existing_user: UserRecord | None = None,
        membership: MembershipRecord | None = None,
    ) -> None:
        self._existing_user = existing_user
        self._membership = membership
        self.created_user: UserRecord | None = None
        self.refresh_tokens: list[dict[str, Any]] = []
        self.last_login_user_id: str | None = None

    async def find_user_by_email(self, session: Any, email: str) -> UserRecord | None:
        return self._existing_user

    async def create_user(self, session: Any, user_id: str, email: str) -> UserRecord:
        self.created_user = UserRecord(id=user_id, email=email, name=None)
        return self.created_user

    async def find_primary_membership(
        self, session: Any, user_id: str
    ) -> MembershipRecord | None:
        return self._membership

    async def touch_last_login(self, session: Any, user_id: str) -> None:
        self.last_login_user_id = user_id

    async def create_refresh_token(
        self,
        session: Any,
        *,
        user_id: str,
        token_hash: bytes,
        family_id: str,
        expires_at: Any,
        user_agent: str | None,
        ip_address: str | None,
    ) -> None:
        self.refresh_tokens.append(
            {
                "user_id": user_id,
                "token_hash": token_hash,
                "family_id": family_id,
                "expires_at": expires_at,
                "user_agent": user_agent,
                "ip_address": ip_address,
            }
        )


@pytest.fixture(autouse=True)
def _patch_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the service's DB session with an in-memory fake."""

    @contextlib.asynccontextmanager
    async def _fake_get_session() -> AsyncIterator[_FakeSession]:
        yield _FakeSession()

    monkeypatch.setattr(service_module, "get_session", _fake_get_session)


def _decode(token: str) -> dict[str, Any]:
    claims: dict[str, Any] = jwt.decode(token, settings.jwt_access_secret, algorithms=["HS256"])
    return claims


def _request(email: str) -> Any:
    # The signup/signin request schemas are structurally identical (single email),
    # so one builder serves both; returned as Any to satisfy either signature.
    return EmailSignupRequest(email=email)


# ── service: signup ──────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_signup_creates_user_and_returns_onboarding() -> None:
    repo = _FakeAuthRepository()
    service = AuthService(repository=repo)

    result = await service.signup(_request("Dana@Riverline.io"), user_agent="ua", ip="1.2.3.4")

    assert result.next_step == "onboarding"
    assert result.workspace is None
    assert result.user.email == "dana@riverline.io"  # normalized to lowercase
    assert repo.created_user is not None

    claims = _decode(result.access_token)
    assert claims["sub"] == result.user.id
    assert claims["workspace_id"] is None
    assert claims["role"] is None
    assert claims["scopes"] == []
    assert claims["exp"] > claims["iat"]


@pytest.mark.asyncio
async def test_signup_stores_only_the_refresh_token_hash() -> None:
    repo = _FakeAuthRepository()
    service = AuthService(repository=repo)

    result = await service.signup(_request("a@b.io"), user_agent=None, ip=None)

    assert len(repo.refresh_tokens) == 1
    stored = repo.refresh_tokens[0]
    # Raw token never written; only its SHA-256 hash is.
    assert stored["token_hash"] == sha256_hash(result.refresh_token)
    assert stored["token_hash"] != result.refresh_token.encode()


@pytest.mark.asyncio
async def test_signup_conflict_when_email_exists() -> None:
    repo = _FakeAuthRepository(
        existing_user=UserRecord(id="usr_1", email="a@b.io", name=None)
    )
    service = AuthService(repository=repo)

    with pytest.raises(ConflictError):
        await service.signup(_request("a@b.io"), user_agent=None, ip=None)


# ── service: signin ──────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_signin_unknown_email_raises_not_found() -> None:
    service = AuthService(repository=_FakeAuthRepository(existing_user=None))

    with pytest.raises(NotFoundError):
        await service.signin(_request("nobody@b.io"), user_agent=None, ip=None)


@pytest.mark.asyncio
async def test_signin_with_workspace_returns_dashboard() -> None:
    repo = _FakeAuthRepository(
        existing_user=UserRecord(id="usr_1", email="dana@riverline.io", name="Dana Reyes"),
        membership=MembershipRecord(
            workspace_id="wrk_1", role="admin",
            workspace_name="Riverline", workspace_slug="riverline",
        ),
    )
    service = AuthService(repository=repo)

    result = await service.signin(_request("dana@riverline.io"), user_agent=None, ip=None)

    assert result.next_step == "dashboard"
    assert result.workspace is not None
    assert result.workspace.slug == "riverline"
    assert repo.last_login_user_id == "usr_1"

    claims = _decode(result.access_token)
    assert claims["workspace_id"] == "wrk_1"
    assert claims["role"] == "admin"


@pytest.mark.asyncio
async def test_signin_without_workspace_returns_onboarding() -> None:
    repo = _FakeAuthRepository(
        existing_user=UserRecord(id="usr_2", email="solo@b.io", name=None),
        membership=None,
    )
    service = AuthService(repository=repo)

    result = await service.signin(_request("solo@b.io"), user_agent=None, ip=None)

    assert result.next_step == "onboarding"
    assert result.workspace is None
    assert _decode(result.access_token)["workspace_id"] is None


# ── router: contract ─────────────────────────────────────────────────────────────
class _StubService:
    def __init__(self, *, result: AuthSessionOut | None = None, error: Exception | None = None):
        self._result = result
        self._error = error

    async def signup(self, body: Any, *, user_agent: str | None, ip: str | None) -> AuthSessionOut:
        return self._respond()

    async def signin(self, body: Any, *, user_agent: str | None, ip: str | None) -> AuthSessionOut:
        return self._respond()

    def _respond(self) -> AuthSessionOut:
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


def _session_out(
    *, workspace: WorkspaceOut | None, next_step: Literal["onboarding", "dashboard"]
) -> AuthSessionOut:
    return AuthSessionOut(
        user=UserOut(id="usr_1", email="dana@riverline.io", name="Dana Reyes"),
        workspace=workspace,
        access_token="access.jwt",
        refresh_token="refresh.raw",
        next_step=next_step,
    )


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    # Import inside the fixture so env defaults from conftest are set first.
    from app.main import app
    from app.shared.middleware.rate_limit import limiter

    limiter.enabled = False  # avoid the Redis-backed limiter in unit tests
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        limiter.enabled = True
        app.dependency_overrides.clear()


def _override(service: _StubService) -> None:
    from app.main import app
    from app.modules.auth.router import get_auth_service

    app.dependency_overrides[get_auth_service] = lambda: service


@pytest.mark.asyncio
async def test_signup_route_returns_201_envelope(client: AsyncClient) -> None:
    _override(_StubService(result=_session_out(workspace=None, next_step="onboarding")))

    resp = await client.post("/api/v1/auth/signup", json={"email": "dana@riverline.io"})

    assert resp.status_code == 201
    body = resp.json()
    assert body["data"]["nextStep"] == "onboarding"
    assert body["data"]["accessToken"] == "access.jwt"
    assert body["data"]["refreshToken"] == "refresh.raw"
    assert body["data"]["workspace"] is None
    assert body["meta"]["requestId"]
    assert resp.headers["X-Request-ID"]


@pytest.mark.asyncio
async def test_signin_route_returns_200_with_workspace(client: AsyncClient) -> None:
    workspace = WorkspaceOut(id="wrk_1", name="Riverline", slug="riverline")
    _override(_StubService(result=_session_out(workspace=workspace, next_step="dashboard")))

    resp = await client.post("/api/v1/auth/signin", json={"email": "dana@riverline.io"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["nextStep"] == "dashboard"
    assert body["data"]["workspace"]["slug"] == "riverline"


@pytest.mark.asyncio
async def test_signup_rejects_invalid_email(client: AsyncClient) -> None:
    _override(_StubService(result=_session_out(workspace=None, next_step="onboarding")))

    resp = await client.post("/api/v1/auth/signup", json={"email": "not-an-email"})

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


@pytest.mark.asyncio
async def test_signup_rejects_unknown_fields(client: AsyncClient) -> None:
    _override(_StubService(result=_session_out(workspace=None, next_step="onboarding")))

    resp = await client.post(
        "/api/v1/auth/signup",
        json={"email": "dana@riverline.io", "password": "hunter2"},
    )

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


@pytest.mark.asyncio
async def test_signup_conflict_maps_to_409(client: AsyncClient) -> None:
    _override(_StubService(error=ConflictError("Email already registered")))

    resp = await client.post("/api/v1/auth/signup", json={"email": "dana@riverline.io"})

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "conflict"


@pytest.mark.asyncio
async def test_signin_unknown_email_maps_to_404(client: AsyncClient) -> None:
    _override(_StubService(error=NotFoundError("Account")))

    resp = await client.post("/api/v1/auth/signin", json={"email": "ghost@b.io"})

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"
