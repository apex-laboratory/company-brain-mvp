"""Auth business logic (BACKEND_BEST_PRACTICES.md §2 layering, §7 tokens).

The service orchestrates the repository and owns token issuance. It is
framework-agnostic (no ``Request``/``Response``): the router passes the request
metadata it needs (``user_agent``, ``ip``) as plain values.

Covers passwordless email ``signup``/``signin`` (KAN-49) and refresh-token
rotation + ``logout`` (KAN-50). All four go through one token-minting path
(:meth:`_persist_session_tokens`), so access-token claims and refresh-token
storage stay consistent across login and rotation.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_session
from app.config.settings import settings
from app.integrations.oauth import OAuthError, OAuthProfile, github_oauth, google_oauth
from app.modules.auth.repository import AuthRepository, MembershipRecord, UserRecord
from app.modules.auth.schemas import (
    AuthSessionOut,
    EmailSigninRequest,
    EmailSignupRequest,
    MeOut,
    OAuthStartOut,
    UserOut,
    WorkspaceOut,
)
from app.modules.auth.tokens import generate_refresh_token, mint_access_token
from app.shared.errors.app_error import AppError, ConflictError, NotFoundError, UnauthorizedError
from app.shared.helpers.crypto import sha256_hash
from app.shared.helpers.ids import generate_id
from app.shared.helpers.oauth_state import decode_state, encode_state
from app.shared.logger import get_logger

log = get_logger()

# Single source of truth for the OAuth providers wired into the code-exchange
# flow. Adding a provider is one entry here; the router derives its allow-list
# from this set, so the URL builder, profile fetch, and validation never drift.
_PROVIDERS = {"google": google_oauth, "github": github_oauth}
OAUTH_PROVIDERS: frozenset[str] = frozenset(_PROVIDERS)
_PROVIDER_CRED_ATTRS: dict[str, tuple[str, str]] = {
    "google": ("login_google_client_id", "login_google_client_secret"),
    "github": ("login_github_client_id", "login_github_client_secret"),
}


@dataclass(frozen=True)
class IssuedTokens:
    """A freshly minted access + refresh token pair (raw values)."""

    access_token: str
    refresh_token: str


class AuthService:
    def __init__(self, repository: AuthRepository | None = None) -> None:
        self._repository = repository or AuthRepository()

    # ── passwordless email auth (KAN-49) ──────────────────────────────────────
    async def signup(
        self,
        request: EmailSignupRequest,
        *,
        user_agent: str | None,
        ip: str | None,
    ) -> AuthSessionOut:
        """Create a new user and an initial session (no workspace yet).

        Raises ``ConflictError`` (409) if the email is already registered.
        """
        email = request.email.lower()
        async with get_session() as session:
            if await self._repository.find_user_by_email(session, email) is not None:
                raise ConflictError("Email already registered")

            try:
                user = await self._repository.create_user(
                    session, generate_id("user"), email
                )
            except IntegrityError as exc:
                # Lost a race to a concurrent signup: the pre-check passed but a
                # parallel request inserted this email first, tripping the
                # users_email_lower unique index. Map to the same 409 as the
                # pre-check rather than letting it surface as a 500.
                await session.rollback()
                raise ConflictError("Email already registered") from exc

            access_token, refresh_token = await self.issue_token_pair(
                session,
                user_id=user.id,
                workspace_id=None,
                role=None,
                user_agent=user_agent,
                ip=ip,
            )
            await self._repository.touch_last_login(session, user.id)
            await session.commit()

        return self._session_response(
            user,
            membership=None,
            access_token=access_token,
            refresh_token=refresh_token,
        )

    async def signin(
        self,
        request: EmailSigninRequest,
        *,
        user_agent: str | None,
        ip: str | None,
    ) -> AuthSessionOut:
        """Return an existing user's session, scoped to their primary workspace.

        Raises ``NotFoundError`` (404) if no account exists for the email. The
        ``next_step`` is ``dashboard`` when the user has a workspace, otherwise
        ``onboarding`` (account exists but onboarding never finished).
        """
        email = request.email.lower()
        async with get_session() as session:
            user = await self._repository.find_user_by_email(session, email)
            if user is None:
                raise NotFoundError("Account")

            membership = await self._repository.find_primary_membership(session, user.id)
            access_token, refresh_token = await self.issue_token_pair(
                session,
                user_id=user.id,
                workspace_id=membership.workspace_id if membership else None,
                role=membership.role if membership else None,
                user_agent=user_agent,
                ip=ip,
            )
            await self._repository.touch_last_login(session, user.id)
            await session.commit()

        return self._session_response(
            user,
            membership=membership,
            access_token=access_token,
            refresh_token=refresh_token,
        )

    @staticmethod
    def _session_response(
        user: UserRecord,
        *,
        membership: MembershipRecord | None,
        access_token: str,
        refresh_token: str,
    ) -> AuthSessionOut:
        """Assemble the session payload shared by signup and signin.

        ``next_step`` derives solely from workspace membership: a user with an
        active workspace lands on the ``dashboard``; one without (a fresh signup,
        or an account that never finished onboarding) goes to ``onboarding``.
        """
        workspace = (
            WorkspaceOut(
                id=membership.workspace_id,
                name=membership.workspace_name,
                slug=membership.workspace_slug,
            )
            if membership is not None
            else None
        )
        return AuthSessionOut(
            user=UserOut(id=user.id, email=user.email, name=user.name),
            workspace=workspace,
            access_token=access_token,
            refresh_token=refresh_token,
            next_step="dashboard" if membership is not None else "onboarding",
        )

    async def me(self, *, user_id: str) -> MeOut:
        """Return the caller's user + primary workspace + role (GET /auth/me).

        The FE calls this on reload to rebuild session state from the access
        token instead of trusting a localStorage snapshot. Mirrors the session
        payload's user/workspace/next_step, plus ``role`` for route guards, and
        omits tokens. Opens its own session (like signup/signin)."""
        async with get_session() as session:
            user = await self._repository.find_user_by_id(session, user_id)
            if user is None:
                raise UnauthorizedError("User not found")
            membership = await self._repository.find_primary_membership(session, user_id)
        workspace = (
            WorkspaceOut(
                id=membership.workspace_id,
                name=membership.workspace_name,
                slug=membership.workspace_slug,
            )
            if membership is not None
            else None
        )
        return MeOut(
            user=UserOut(id=user.id, email=user.email, name=user.name),
            workspace=workspace,
            role=membership.role if membership is not None else None,
            next_step="dashboard" if membership is not None else "onboarding",
        )

    # ── shared issuance ───────────────────────────────────────────────────────
    async def issue_token_pair(
        self,
        session: AsyncSession,
        *,
        user_id: str,
        workspace_id: str | None,
        role: str | None,
        user_agent: str | None,
        ip: str | None,
    ) -> tuple[str, str]:
        """Start a brand-new session in a fresh rotation family. Caller commits.

        Used by signup/signin (and OAuth/workspace-creation in sibling tickets).
        Refresh rotation reuses the same family via :meth:`refresh`.
        """
        family_id = generate_id("refresh_token")
        access_token, raw_refresh, _ = await self._persist_session_tokens(
            session,
            user_id=user_id,
            workspace_id=workspace_id,
            role=role,
            family_id=family_id,
            user_agent=user_agent,
            ip=ip,
        )
        return access_token, raw_refresh

    # ── refresh rotation (KAN-50) ──────────────────────────────────────────────
    async def refresh(
        self,
        session: AsyncSession,
        *,
        raw_token: str,
        user_agent: str | None = None,
        ip: str | None = None,
    ) -> IssuedTokens:
        """Rotate a refresh token, returning a new pair.

        Enforces, in order: existence, family reuse detection, expiry. On
        success the presented token is revoked and linked to its replacement.
        Every failure surfaces as a generic 401 so the response never reveals
        which check failed.
        """
        # Lock the row so concurrent rotations of the same token serialize.
        row = await self._repository.find_by_token_hash(
            session, sha256_hash(raw_token), for_update=True
        )

        if row is None:
            raise UnauthorizedError("Invalid refresh token")

        if row.revoked_at is not None:
            # A revoked token was replayed — treat the whole family as compromised.
            await self.revoke_family(session, row.family_id)
            await session.commit()
            log.warning(
                "refresh_token_reuse_detected",
                user_id=row.user_id,
                family_id=row.family_id,
            )
            raise UnauthorizedError(
                "Session invalidated due to token reuse. Please sign in again."
            )

        if row.expires_at < datetime.now(UTC):
            raise UnauthorizedError("Refresh token expired")

        # Claims reflect the user's *current* membership, so role changes take
        # effect on the next refresh. None before onboarding.
        membership = await self._repository.find_primary_membership(session, row.user_id)
        access_token, raw_refresh, new_token_id = await self._persist_session_tokens(
            session,
            user_id=row.user_id,
            workspace_id=membership.workspace_id if membership else None,
            role=membership.role if membership else None,
            family_id=row.family_id,
            user_agent=user_agent,
            ip=ip,
        )
        await self._repository.revoke_token(
            session, token_id=row.id, replaced_by=new_token_id
        )
        await session.commit()
        return IssuedTokens(access_token=access_token, refresh_token=raw_refresh)

    # ── logout (KAN-50) ────────────────────────────────────────────────────────
    async def logout(self, session: AsyncSession, *, raw_token: str) -> None:
        """Revoke the presented refresh token. Idempotent: an unknown or
        already-revoked token silently succeeds (no token-probing oracle)."""
        row = await self._repository.find_by_token_hash(session, sha256_hash(raw_token))
        if row is not None and row.revoked_at is None:
            await self._repository.revoke_token(session, token_id=row.id)
        await session.commit()

    async def revoke_family(self, session: AsyncSession, family_id: str) -> None:
        """Revoke every live token in a rotation family."""
        await self._repository.revoke_family(session, family_id)

    # ── OAuth start (KAN-40) ──────────────────────────────────────────────────
    async def start_oauth(
        self,
        *,
        provider: str,
        mode: str,
        user_id: str | None,
        workspace_id: str | None,
    ) -> OAuthStartOut:
        """Generate a signed state, persist it, and return the provider's auth URL.

        SAML is not yet supported and raises 501. ``user_id``/``workspace_id``
        come from the caller's current JWT if they are already authenticated
        (account-linking flow); both are ``None`` for sign-up/sign-in flows.
        """
        if provider == "saml":
            raise AppError(501, "not_implemented", "SAML is not yet supported.")

        # Login SSO is frontend-driven: the provider redirects the browser to the
        # FE callback page (not the backend), which then POSTs {code, state} to
        # /auth/oauth/{provider}/callback. So the redirect_uri registered with the
        # provider — and echoed here + in the token exchange — is the FE page.
        redirect_uri = f"{settings.frontend_url}{settings.frontend_oauth_callback_path}"

        expires_at = datetime.now(UTC) + timedelta(
            seconds=settings.oauth_state_ttl_seconds
        )
        state_raw = encode_state(
            user_id=user_id,
            workspace_id=workspace_id,
            provider=provider,
            redirect_uri=redirect_uri,
            mode=mode,
            expires_at=expires_at,
        )
        state_hash = sha256_hash(state_raw)

        async with get_session() as session:
            await self._repository.create_oauth_state(
                session,
                state_hash=state_hash,
                provider=provider,
                redirect_uri=redirect_uri,
                expires_at=expires_at,
                user_id=user_id,
                workspace_id=workspace_id,
            )
            await session.commit()

        auth_url = _build_auth_url(provider, state_raw, redirect_uri)
        return OAuthStartOut(authorization_url=auth_url, state=state_raw)

    # ── OAuth callback (KAN-40) ───────────────────────────────────────────────
    async def handle_oauth_callback(
        self,
        *,
        provider: str,
        code: str,
        state: str,
        current_user_id: str | None,
        user_agent: str | None,
        ip: str | None,
    ) -> AuthSessionOut:
        """Verify state, exchange code, upsert user, issue tokens.

        All six state-validation checks must pass; any failure raises a generic
        401 so callers cannot enumerate which check failed.
        """
        # Checks 1 & 2: JWT signature valid + exp not in the past (shared helper).
        state_claims = decode_state(state)

        state_hash = sha256_hash(state)

        # ── Phase 1: validate + consume the state (short transaction) ──────────
        # The state is consumed and committed here, before the provider round-trip,
        # so we never hold a DB connection or the ``FOR UPDATE`` row lock across an
        # external HTTP call (which can take up to ~20s for GitHub). The lock still
        # serialises concurrent callbacks for the same state: the second request
        # blocks on the SELECT FOR UPDATE until this commits, then sees
        # ``consumed_at`` set and fails check 4. A failed exchange afterwards leaves
        # the state burned (codes are single-use at the provider anyway).
        state_user_id = state_claims.get("user_id")
        async with get_session() as session:
            # Check 3: row exists by sha256(state).
            row = await self._repository.find_oauth_state(
                session, state_hash, for_update=True
            )
            if row is None:
                raise UnauthorizedError("Invalid OAuth state")

            # Check 4: not already consumed (single-use).
            if row.consumed_at is not None:
                raise UnauthorizedError("Invalid OAuth state")

            # Check 5: provider in state matches :provider path param.
            if state_claims.get("provider") != provider:
                raise UnauthorizedError("Invalid OAuth state")

            # Check 6: redirect_uri in state matches the stored value.
            if state_claims.get("redirect_uri") != row.redirect_uri:
                raise UnauthorizedError("Invalid OAuth state")

            # Check 7: if user_id present in state, it must match the
            # currently authenticated user (if any).
            if (
                state_user_id is not None
                and current_user_id is not None
                and state_user_id != current_user_id
            ):
                raise UnauthorizedError("Invalid OAuth state")

            redirect_uri = row.redirect_uri
            await self._repository.mark_oauth_state_consumed(session, row.id)
            await session.commit()

        # ── Phase 2: provider round-trip (no DB connection held) ───────────────
        try:
            profile = await _fetch_profile(provider, code, redirect_uri)
        except httpx.HTTPError as exc:
            log.warning("oauth_provider_error", provider=provider, error=str(exc))
            raise AppError(
                502, "provider_error", "OAuth provider request failed."
            ) from exc
        except OAuthError as exc:
            # HTTP succeeded but the payload is unusable (error body, unverified or
            # missing email). Surface the provider-supplied status/code, not a 500.
            log.warning("oauth_profile_error", provider=provider, error=str(exc))
            raise AppError(exc.status, exc.code, exc.message) from exc

        # ── Phase 3: upsert user + issue tokens (second transaction) ───────────
        async with get_session() as session:
            # Upsert the user row (no duplicate users for the same email).
            new_user_id = generate_id("user")
            user = await self._repository.upsert_oauth_user(
                session,
                user_id=new_user_id,
                email=profile.email.lower(),
                name=profile.name,
            )

            # Account-linking guard: if the state was minted for an authenticated
            # user (``state_user_id``), the provider email must resolve to that same
            # user. Without this, a linking attempt whose provider email differs
            # would silently log the caller into (or create) a *different* account.
            # Proper cross-email linking needs a provider-identity table; until then
            # we fail closed rather than switch identity. The rollback undoes the
            # throwaway upsert when the emails don't match.
            if state_user_id is not None and user.id != state_user_id:
                raise UnauthorizedError("Invalid OAuth state")

            membership = await self._repository.find_primary_membership(
                session, user.id
            )
            access_token, refresh_token = await self.issue_token_pair(
                session,
                user_id=user.id,
                workspace_id=membership.workspace_id if membership else None,
                role=membership.role if membership else None,
                user_agent=user_agent,
                ip=ip,
            )
            await self._repository.touch_last_login(session, user.id)
            await session.commit()

        return self._session_response(
            user,
            membership=membership,
            access_token=access_token,
            refresh_token=refresh_token,
        )

    # ── internals ─────────────────────────────────────────────────────────────
    async def _persist_session_tokens(
        self,
        session: AsyncSession,
        *,
        user_id: str,
        workspace_id: str | None,
        role: str | None,
        family_id: str,
        user_agent: str | None,
        ip: str | None,
    ) -> tuple[str, str, str]:
        """Mint an access token and persist a new refresh-token row (hashed).

        Returns ``(access_token, raw_refresh_token, refresh_token_id)``. The id
        lets the rotation caller link the old row's ``replaced_by``. Does not
        commit or revoke anything.
        """
        access_token = mint_access_token(
            user_id=user_id, workspace_id=workspace_id, role=role
        )
        raw_refresh = generate_refresh_token()
        expires_at = datetime.now(UTC) + timedelta(
            seconds=settings.refresh_token_ttl_seconds
        )
        token_id = await self._repository.insert_refresh_token(
            session,
            user_id=user_id,
            token_hash=sha256_hash(raw_refresh),
            family_id=family_id,
            expires_at=expires_at,
            user_agent=user_agent,
            ip_address=ip,
        )
        return access_token, raw_refresh, token_id


# ── module-level helpers ───────────────────────────────────────────────────────

def _provider_credentials(provider: str) -> tuple[str, str]:
    """Resolve ``(client_id, client_secret)`` from settings for a provider."""
    id_attr, secret_attr = _PROVIDER_CRED_ATTRS[provider]
    return getattr(settings, id_attr), getattr(settings, secret_attr)


def _build_auth_url(provider: str, state_raw: str, redirect_uri: str) -> str:
    """Construct the provider's authorization URL with required query params."""
    client_id, _ = _provider_credentials(provider)
    return _PROVIDERS[provider].build_authorize_url(
        client_id=client_id, redirect_uri=redirect_uri, state=state_raw
    )


async def _fetch_profile(
    provider: str, code: str, redirect_uri: str
) -> OAuthProfile:
    """Dispatch code exchange + profile fetch to the correct provider module."""
    module = _PROVIDERS[provider]
    client_id, client_secret = _provider_credentials(provider)
    token = await module.exchange_code(
        code=code,
        redirect_uri=redirect_uri,
        client_id=client_id,
        client_secret=client_secret,
    )
    return await module.fetch_profile(token)
