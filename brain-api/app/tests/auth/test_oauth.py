"""Unit tests for the OAuth SSO flow (KAN-40).

Provider HTTP calls are monkeypatched; the repository is replaced with an
in-memory fake. No database or Redis required.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from jose import jwt

from app.config.settings import settings
from app.integrations.oauth import OAuthError, OAuthProfile
from app.integrations.oauth.github_oauth import _primary_email
from app.modules.auth.repository import (
    AuthRepository,
    MembershipRecord,
    OAuthStateRow,
    UserRecord,
)
from app.modules.auth.service import AuthService
from app.shared.errors.app_error import AppError, UnauthorizedError
from app.shared.helpers.crypto import sha256_hash

# ── fake session ──────────────────────────────────────────────────────────────

class FakeSession:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


# ── fake repository ───────────────────────────────────────────────────────────

@dataclass
class _StateRow:
    id: str
    user_id: str | None
    workspace_id: str | None
    provider: str
    redirect_uri: str
    state_hash: bytes
    expires_at: datetime
    consumed_at: datetime | None = None


@dataclass
class _TokenRow:
    id: str
    user_id: str
    token_hash: bytes
    family_id: str
    expires_at: datetime
    revoked_at: datetime | None = None
    replaced_by: str | None = None


@dataclass
class FakeRepo(AuthRepository):
    state_rows: list[_StateRow] = field(default_factory=list)
    token_rows: list[_TokenRow] = field(default_factory=list)
    users: dict[str, UserRecord] = field(default_factory=dict)  # email -> record
    membership: MembershipRecord | None = None
    _counter: int = 0

    # ── oauth_states ──────────────────────────────────────────────────────────
    async def create_oauth_state(
        self,
        session: Any,
        *,
        state_hash: bytes,
        provider: str,
        redirect_uri: str,
        expires_at: datetime,
        user_id: str | None,
        workspace_id: str | None,
    ) -> None:
        self._counter += 1
        self.state_rows.append(
            _StateRow(
                id=f"os-{self._counter}",
                user_id=user_id,
                workspace_id=workspace_id,
                provider=provider,
                redirect_uri=redirect_uri,
                state_hash=state_hash,
                expires_at=expires_at,
            )
        )

    async def find_oauth_state(
        self,
        session: Any,
        state_hash: bytes,
        *,
        for_update: bool = False,
    ) -> OAuthStateRow | None:
        for r in self.state_rows:
            if r.state_hash == state_hash:
                return OAuthStateRow(
                    id=r.id,
                    user_id=r.user_id,
                    workspace_id=r.workspace_id,
                    provider=r.provider,
                    redirect_uri=r.redirect_uri,
                    expires_at=r.expires_at,
                    consumed_at=r.consumed_at,
                )
        return None

    async def mark_oauth_state_consumed(self, session: Any, state_id: str) -> None:
        for r in self.state_rows:
            if r.id == state_id and r.consumed_at is None:
                r.consumed_at = datetime.now(UTC)

    # ── users ─────────────────────────────────────────────────────────────────
    async def upsert_oauth_user(
        self,
        session: Any,
        *,
        user_id: str,
        email: str,
        name: str | None,
    ) -> UserRecord:
        existing = self.users.get(email.lower())
        if existing is not None:
            return existing
        record = UserRecord(id=user_id, email=email, name=name)
        self.users[email.lower()] = record
        return record

    async def find_primary_membership(
        self, session: Any, user_id: str
    ) -> MembershipRecord | None:
        return self.membership

    async def touch_last_login(self, session: Any, user_id: str) -> None:
        pass

    # ── refresh tokens ────────────────────────────────────────────────────────
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
        row_id = f"rt-{self._counter}"
        self.token_rows.append(
            _TokenRow(
                id=row_id,
                user_id=user_id,
                token_hash=token_hash,
                family_id=family_id,
                expires_at=expires_at,
            )
        )
        return row_id


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_state(
    *,
    provider: str = "google",
    mode: str = "signup",
    user_id: str | None = None,
    workspace_id: str | None = None,
    redirect_uri: str = "http://localhost:4000/api/v1/auth/oauth/google/callback",
    ttl_seconds: int = 600,
) -> str:
    """Mint a valid state JWT signed with the test secret."""
    now = datetime.now(UTC)
    payload: dict[str, object] = {
        "user_id": user_id,
        "workspace_id": workspace_id,
        "provider": provider,
        "redirect_uri": redirect_uri,
        "mode": mode,
        "exp": int((now + timedelta(seconds=ttl_seconds)).timestamp()),
    }
    state: str = jwt.encode(payload, settings.jwt_access_secret, algorithm="HS256")
    return state


def _seed_state(
    repo: FakeRepo,
    state_raw: str,
    *,
    provider: str = "google",
    redirect_uri: str = "http://localhost:4000/api/v1/auth/oauth/google/callback",
    consumed: bool = False,
    expired: bool = False,
) -> _StateRow:
    """Insert an oauth_states row into the fake repo and return it."""
    expires_at = (
        datetime.now(UTC) - timedelta(seconds=1)
        if expired
        else datetime.now(UTC) + timedelta(minutes=10)
    )
    row = _StateRow(
        id=f"os-seed-{len(repo.state_rows)}",
        user_id=None,
        workspace_id=None,
        provider=provider,
        redirect_uri=redirect_uri,
        state_hash=sha256_hash(state_raw),
        expires_at=expires_at,
        consumed_at=datetime.now(UTC) if consumed else None,
    )
    repo.state_rows.append(row)
    return row


_GOOGLE_PROFILE = OAuthProfile(email="alice@example.com", name="Alice")


async def _noop_fetch(provider: str, code: str, redirect_uri: str) -> OAuthProfile:
    return _GOOGLE_PROFILE


# ── start_oauth ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_start_google_returns_authorization_url_and_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = FakeRepo()
    service = AuthService(repository=repo)

    monkeypatch.setattr("app.modules.auth.service.get_session", _fake_session_ctx)

    result = await service.start_oauth(
        provider="google", mode="signup", user_id=None, workspace_id=None
    )

    assert "accounts.google.com" in result.authorization_url
    assert result.state  # non-empty JWT
    # DB row was created
    assert len(repo.state_rows) == 1
    assert repo.state_rows[0].provider == "google"
    assert repo.state_rows[0].consumed_at is None


@pytest.mark.asyncio
async def test_start_github_returns_authorization_url_and_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = FakeRepo()
    service = AuthService(repository=repo)

    monkeypatch.setattr("app.modules.auth.service.get_session", _fake_session_ctx)

    result = await service.start_oauth(
        provider="github", mode="signin", user_id=None, workspace_id=None
    )

    assert "github.com/login/oauth/authorize" in result.authorization_url
    # urlencode percent-encodes the colon in "user:email"
    assert "user%3Aemail" in result.authorization_url


@pytest.mark.asyncio
async def test_start_uses_frontend_callback_redirect_uri(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from urllib.parse import quote

    repo = FakeRepo()
    service = AuthService(repository=repo)
    monkeypatch.setattr("app.modules.auth.service.get_session", _fake_session_ctx)

    result = await service.start_oauth(
        provider="google", mode="signin", user_id=None, workspace_id=None
    )

    # Login is frontend-driven: the redirect_uri sent to the provider is the FE
    # callback page, not a backend route.
    expected = quote(
        f"{settings.frontend_url}{settings.frontend_oauth_callback_path}", safe=""
    )
    assert expected in result.authorization_url
    assert "%2Fapi%2Fv1%2Fauth" not in result.authorization_url


@pytest.mark.asyncio
async def test_start_saml_raises_501() -> None:
    service = AuthService(repository=FakeRepo())

    with pytest.raises(AppError) as exc:
        await service.start_oauth(
            provider="saml", mode="signin", user_id=None, workspace_id=None
        )
    assert exc.value.status == 501


@pytest.mark.asyncio
async def test_start_creates_state_row_with_correct_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = FakeRepo()
    service = AuthService(repository=repo)

    monkeypatch.setattr("app.modules.auth.service.get_session", _fake_session_ctx)

    result = await service.start_oauth(
        provider="google", mode="signup", user_id=None, workspace_id=None
    )

    stored_hash = repo.state_rows[0].state_hash
    assert stored_hash == sha256_hash(result.state)


# ── handle_oauth_callback — happy path ───────────────────────────────────────

@pytest.mark.asyncio
async def test_callback_happy_path_google(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = FakeRepo()
    service = AuthService(repository=repo)
    state_raw = _make_state(provider="google")
    _seed_state(repo, state_raw, provider="google")

    monkeypatch.setattr("app.modules.auth.service.get_session", _fake_session_ctx)
    monkeypatch.setattr("app.modules.auth.service._fetch_profile", _noop_fetch)

    out = await service.handle_oauth_callback(
        provider="google",
        code="auth-code",
        state=state_raw,
        current_user_id=None,
        user_agent=None,
        ip=None,
    )

    assert out.user.email == "alice@example.com"
    assert out.access_token
    assert out.refresh_token
    # State row must be consumed
    assert repo.state_rows[0].consumed_at is not None
    # A refresh token was minted
    assert len(repo.token_rows) == 1


@pytest.mark.asyncio
async def test_callback_upserts_existing_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = UserRecord(id="usr_existing", email="alice@example.com", name="Alice")
    repo = FakeRepo(users={"alice@example.com": existing})
    service = AuthService(repository=repo)
    state_raw = _make_state(provider="google")
    _seed_state(repo, state_raw, provider="google")

    monkeypatch.setattr("app.modules.auth.service.get_session", _fake_session_ctx)
    monkeypatch.setattr("app.modules.auth.service._fetch_profile", _noop_fetch)

    out = await service.handle_oauth_callback(
        provider="google",
        code="code",
        state=state_raw,
        current_user_id=None,
        user_agent=None,
        ip=None,
    )

    # Same user returned, no duplicate
    assert out.user.id == "usr_existing"
    assert len(repo.users) == 1


# ── handle_oauth_callback — state validation failures ─────────────────────────

@pytest.mark.asyncio
async def test_callback_invalid_jwt_signature_raises_401() -> None:
    service = AuthService(repository=FakeRepo())

    with pytest.raises(UnauthorizedError) as exc:
        await service.handle_oauth_callback(
            provider="google",
            code="code",
            state="not.a.jwt",
            current_user_id=None,
            user_agent=None,
            ip=None,
        )
    assert exc.value.status == 401
    assert exc.value.message == "Invalid OAuth state"


@pytest.mark.asyncio
async def test_callback_expired_state_raises_401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = FakeRepo()
    service = AuthService(repository=repo)
    # JWT is already expired (ttl=-1)
    expired_state = _make_state(provider="google", ttl_seconds=-1)
    _seed_state(repo, expired_state, provider="google")

    monkeypatch.setattr("app.modules.auth.service.get_session", _fake_session_ctx)

    with pytest.raises(UnauthorizedError) as exc:
        await service.handle_oauth_callback(
            provider="google",
            code="code",
            state=expired_state,
            current_user_id=None,
            user_agent=None,
            ip=None,
        )
    assert exc.value.status == 401


@pytest.mark.asyncio
async def test_callback_missing_state_row_raises_401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = FakeRepo()  # no state rows seeded
    service = AuthService(repository=repo)
    state_raw = _make_state(provider="google")

    monkeypatch.setattr("app.modules.auth.service.get_session", _fake_session_ctx)

    with pytest.raises(UnauthorizedError) as exc:
        await service.handle_oauth_callback(
            provider="google",
            code="code",
            state=state_raw,
            current_user_id=None,
            user_agent=None,
            ip=None,
        )
    assert exc.value.status == 401


@pytest.mark.asyncio
async def test_callback_already_consumed_raises_401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = FakeRepo()
    service = AuthService(repository=repo)
    state_raw = _make_state(provider="google")
    _seed_state(repo, state_raw, provider="google", consumed=True)

    monkeypatch.setattr("app.modules.auth.service.get_session", _fake_session_ctx)

    with pytest.raises(UnauthorizedError) as exc:
        await service.handle_oauth_callback(
            provider="google",
            code="code",
            state=state_raw,
            current_user_id=None,
            user_agent=None,
            ip=None,
        )
    assert exc.value.status == 401


@pytest.mark.asyncio
async def test_callback_wrong_provider_raises_401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """State was created for 'google' but callback arrives on 'github' path."""
    repo = FakeRepo()
    service = AuthService(repository=repo)
    state_raw = _make_state(provider="google")
    _seed_state(repo, state_raw, provider="google")

    monkeypatch.setattr("app.modules.auth.service.get_session", _fake_session_ctx)

    with pytest.raises(UnauthorizedError) as exc:
        await service.handle_oauth_callback(
            provider="github",  # mismatch
            code="code",
            state=state_raw,
            current_user_id=None,
            user_agent=None,
            ip=None,
        )
    assert exc.value.status == 401


@pytest.mark.asyncio
async def test_callback_redirect_uri_mismatch_raises_401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DB row has a different redirect_uri than the one encoded in the state JWT."""
    repo = FakeRepo()
    service = AuthService(repository=repo)
    state_raw = _make_state(
        provider="google",
        redirect_uri="http://localhost:4000/api/v1/auth/oauth/google/callback",
    )
    # Seed with a different redirect_uri in the DB row.
    row = _seed_state(repo, state_raw, provider="google")
    row.redirect_uri = "https://evil.example.com/callback"

    monkeypatch.setattr("app.modules.auth.service.get_session", _fake_session_ctx)

    with pytest.raises(UnauthorizedError) as exc:
        await service.handle_oauth_callback(
            provider="google",
            code="code",
            state=state_raw,
            current_user_id=None,
            user_agent=None,
            ip=None,
        )
    assert exc.value.status == 401


@pytest.mark.asyncio
async def test_callback_user_id_mismatch_raises_401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """State carries user_id='usr_A' but authenticated caller is 'usr_B'."""
    repo = FakeRepo()
    service = AuthService(repository=repo)
    state_raw = _make_state(provider="google", user_id="usr_A")
    _seed_state(repo, state_raw, provider="google")

    monkeypatch.setattr("app.modules.auth.service.get_session", _fake_session_ctx)

    with pytest.raises(UnauthorizedError) as exc:
        await service.handle_oauth_callback(
            provider="google",
            code="code",
            state=state_raw,
            current_user_id="usr_B",  # different user
            user_agent=None,
            ip=None,
        )
    assert exc.value.status == 401


@pytest.mark.asyncio
async def test_callback_user_id_in_state_none_skips_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No user_id in state → check 7 is vacuously satisfied even with auth'd caller."""
    repo = FakeRepo()
    service = AuthService(repository=repo)
    state_raw = _make_state(provider="google", user_id=None)
    _seed_state(repo, state_raw, provider="google")

    monkeypatch.setattr("app.modules.auth.service.get_session", _fake_session_ctx)
    monkeypatch.setattr("app.modules.auth.service._fetch_profile", _noop_fetch)

    out = await service.handle_oauth_callback(
        provider="google",
        code="code",
        state=state_raw,
        current_user_id="usr_any",  # should not cause a failure
        user_agent=None,
        ip=None,
    )
    assert out.user.email == "alice@example.com"


# ── provider profile validation ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_callback_maps_oauth_error_to_provider_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An OAuthError from the provider leg (e.g. unverified email) becomes a clean
    AppError with the provider-supplied status — never an unhandled 500."""
    repo = FakeRepo()
    service = AuthService(repository=repo)
    state_raw = _make_state(provider="google")
    _seed_state(repo, state_raw, provider="google")

    async def _raise_unverified(provider: str, code: str, redirect_uri: str) -> OAuthProfile:
        raise OAuthError(
            "Your Google email is not verified.",
            status=403,
            code="oauth_email_unverified",
        )

    monkeypatch.setattr("app.modules.auth.service.get_session", _fake_session_ctx)
    monkeypatch.setattr("app.modules.auth.service._fetch_profile", _raise_unverified)

    with pytest.raises(AppError) as exc:
        await service.handle_oauth_callback(
            provider="google",
            code="code",
            state=state_raw,
            current_user_id=None,
            user_agent=None,
            ip=None,
        )
    assert exc.value.status == 403
    assert exc.value.code == "oauth_email_unverified"
    # The state was still consumed in phase 1 (single-use), even though the
    # provider leg failed.
    assert repo.state_rows[0].consumed_at is not None


def test_primary_email_requires_verified() -> None:
    # Primary + verified wins.
    assert (
        _primary_email(
            [
                {"email": "old@x.com", "primary": False, "verified": True},
                {"email": "main@x.com", "primary": True, "verified": True},
            ]
        )
        == "main@x.com"
    )
    # No verified primary → first verified non-primary.
    assert (
        _primary_email(
            [
                {"email": "main@x.com", "primary": True, "verified": False},
                {"email": "alt@x.com", "primary": False, "verified": True},
            ]
        )
        == "alt@x.com"
    )
    # No verified email at all → reject rather than trust an unverified address.
    with pytest.raises(OAuthError):
        _primary_email([{"email": "main@x.com", "primary": True, "verified": False}])
    # Empty list → reject (no IndexError).
    with pytest.raises(OAuthError):
        _primary_email([])


# ── failure messages are generic (no enumeration) ─────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["wrong_signature", "consumed"])
async def test_all_state_failures_return_same_message(
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    """Distinct state-validation failures surface the same generic 401 message,
    so a caller cannot enumerate which check failed."""
    repo = FakeRepo()
    service = AuthService(repository=repo)
    monkeypatch.setattr("app.modules.auth.service.get_session", _fake_session_ctx)

    if scenario == "wrong_signature":
        # check 1/2: not a valid signed JWT.
        state = "not.a.jwt"
    else:
        # check 4: a real, signed state whose row is already consumed.
        state = _make_state(provider="google")
        _seed_state(repo, state, provider="google", consumed=True)

    with pytest.raises(UnauthorizedError) as exc:
        await service.handle_oauth_callback(
            provider="google",
            code="code",
            state=state,
            current_user_id=None,
            user_agent=None,
            ip=None,
        )
    assert exc.value.status == 401
    assert exc.value.message == "Invalid OAuth state"


@asynccontextmanager
async def _fake_session_ctx() -> AsyncGenerator[FakeSession, None]:
    """Drop-in for ``get_session()`` that yields a commit-tracking fake."""
    yield FakeSession()  # type: ignore[misc]
