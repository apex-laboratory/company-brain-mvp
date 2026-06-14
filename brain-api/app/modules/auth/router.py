"""Auth router.

Mounted under ``/api/v1/auth`` by ``main.py``. Owns the passwordless email
endpoints (KAN-49) and refresh-token rotation + logout (KAN-50); the remaining
Auth/Onboarding routes (OAuth) are added by sibling endpoint tickets.

Every route is rate limited to ``AUTH_LIMIT`` (10/min) keyed on client IP — the
limiter's default ``key_func`` is ``get_remote_address`` — to blunt email
enumeration and token-minting/brute-force abuse.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Body, Depends, Request, Response
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config.database import get_session
from app.config.settings import settings
from app.modules.auth.schemas import (
    EmailSigninRequest,
    EmailSignupRequest,
    LogoutRequest,
    OAuthCallbackRequest,
    RefreshRequest,
    TokenPairOut,
)
from app.modules.auth.service import AuthService
from app.shared.errors.app_error import UnauthorizedError
from app.shared.helpers.crypto import sha256_hash
from app.shared.http.respond import created, error_response, no_content, ok
from app.shared.logger import get_logger
from app.shared.middleware.authenticate import _from_jwt
from app.shared.middleware.rate_limit import AUTH_LIMIT, OAUTH_CALLBACK_LIMIT, limiter, user_key

log = get_logger()

router = APIRouter(prefix="/auth", tags=["auth"])

_VALID_PROVIDERS = frozenset({"google", "github", "saml"})
_bearer = HTTPBearer(auto_error=False)

# The raw refresh token is delivered as an httpOnly, secure, samesite=strict
# cookie (BACKEND_BEST_PRACTICES.md §7) so it is never readable by JS. Scoped to
# the auth path so it is only sent where it is needed.
_REFRESH_COOKIE = "refresh_token"
_REFRESH_COOKIE_PATH = "/api/v1/auth"


def get_auth_service() -> AuthService:
    """Provider so the service can be overridden in tests."""
    return AuthService()


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client is not None else None


def _token_from(body: RefreshRequest | LogoutRequest | None, request: Request) -> str | None:
    """Resolve the refresh token from the JSON body, falling back to the cookie.

    The httpOnly-cookie flow sends an empty body (JS cannot read the cookie), so
    the body must be optional and the cookie consulted second.
    """
    from_body = body.refresh_token if body is not None else None
    return from_body or request.cookies.get(_REFRESH_COOKIE)


def _set_refresh_cookie(response: Response, raw_token: str) -> None:
    response.set_cookie(
        _REFRESH_COOKIE,
        raw_token,
        max_age=settings.refresh_token_ttl_seconds,
        httponly=True,
        secure=True,
        samesite="strict",
        path=_REFRESH_COOKIE_PATH,
    )


def _refresh_failed(request: Request, exc: UnauthorizedError) -> Response:
    """401 response that also clears the (now-invalid) refresh cookie, so a
    cookie-based client stops re-presenting a dead token in a loop."""
    log.warning("request_rejected", error_code=exc.code)
    response = error_response(
        request, status=exc.status, code=exc.code, message=exc.message
    )
    response.delete_cookie(_REFRESH_COOKIE, path=_REFRESH_COOKIE_PATH)
    return response


@router.post("/signup", status_code=201)
@limiter.limit(AUTH_LIMIT)
async def signup(
    request: Request,
    body: EmailSignupRequest,
    service: AuthService = Depends(get_auth_service),
) -> JSONResponse:
    session = await service.signup(
        body,
        user_agent=request.headers.get("user-agent"),
        ip=_client_ip(request),
    )
    return created(request, session.model_dump(by_alias=True))


@router.post("/signin")
@limiter.limit(AUTH_LIMIT)
async def signin(
    request: Request,
    body: EmailSigninRequest,
    service: AuthService = Depends(get_auth_service),
) -> JSONResponse:
    session = await service.signin(
        body,
        user_agent=request.headers.get("user-agent"),
        ip=_client_ip(request),
    )
    return ok(request, session.model_dump(by_alias=True))


@router.post("/refresh")
@limiter.limit(AUTH_LIMIT)
async def refresh(
    request: Request,
    body: RefreshRequest | None = Body(default=None),
    service: AuthService = Depends(get_auth_service),
) -> Response:
    """Rotate the refresh token and return a new access + refresh pair.

    The token may arrive in the JSON body (``refreshToken``) or — for the
    httpOnly-cookie flow (BACKEND_BEST_PRACTICES.md §7), where JS cannot read it
    — from the ``refresh_token`` cookie with an empty body.
    """
    raw_token = _token_from(body, request)
    if not raw_token:
        return _refresh_failed(request, UnauthorizedError("Invalid refresh token"))

    async with get_session() as session:
        try:
            issued = await service.refresh(
                session,
                raw_token=raw_token,
                user_agent=request.headers.get("user-agent"),
                ip=_client_ip(request),
            )
        except UnauthorizedError as exc:
            return _refresh_failed(request, exc)

    payload = TokenPairOut(
        access_token=issued.access_token,
        refresh_token=issued.refresh_token,
    ).model_dump(by_alias=True)
    response = ok(request, payload)
    _set_refresh_cookie(response, issued.refresh_token)
    return response


@router.post("/logout", status_code=204)
@limiter.limit(AUTH_LIMIT)
async def logout(
    request: Request,
    body: LogoutRequest | None = Body(default=None),
    service: AuthService = Depends(get_auth_service),
) -> Response:
    """Revoke the presented refresh token. Idempotent — always returns 204."""
    raw_token = _token_from(body, request)
    if raw_token:
        async with get_session() as session:
            await service.logout(session, raw_token=raw_token)

    response = no_content()
    response.delete_cookie(_REFRESH_COOKIE, path=_REFRESH_COOKIE_PATH)
    return response


# ── OAuth SSO (KAN-40) ─────────────────────────────────────────────────────────

@router.get("/oauth/{provider}/start")
@limiter.limit(AUTH_LIMIT)
async def oauth_start(
    request: Request,
    provider: str,
    mode: Literal["signup", "signin"] = "signin",
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    service: AuthService = Depends(get_auth_service),
) -> JSONResponse:
    """Return a signed state token and the provider's authorization URL.

    Accepts an optional Bearer token so an already-authenticated user can link
    an additional provider (account-linking). Unauthenticated callers get
    ``user_id=None`` in the state.
    """
    if provider not in _VALID_PROVIDERS:
        return error_response(
            request, status=400, code="invalid_provider",
            message="Provider must be google, github, or saml.",
        )

    auth = _from_jwt(credentials.credentials) if credentials else None
    result = await service.start_oauth(
        provider=provider,
        mode=mode,
        user_id=auth.user_id if auth else None,
        workspace_id=auth.workspace_id if auth else None,
    )
    return ok(request, result.model_dump(by_alias=True))


@router.post("/oauth/{provider}/callback")
@limiter.limit(OAUTH_CALLBACK_LIMIT, key_func=user_key)
async def oauth_callback(
    request: Request,
    provider: str,
    body: OAuthCallbackRequest,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    service: AuthService = Depends(get_auth_service),
) -> JSONResponse:
    """Exchange an authorization code for a Brainite session.

    Verifies all six state checks before touching the provider. Any failure
    returns a generic 401 — never revealing which check failed.
    """
    if provider not in _VALID_PROVIDERS:
        return error_response(
            request, status=400, code="invalid_provider",
            message="Provider must be google, github, or saml.",
        )

    auth = _from_jwt(credentials.credentials) if credentials else None
    session_out = await service.handle_oauth_callback(
        provider=provider,
        code=body.code,
        state=body.state,
        current_user_id=auth.user_id if auth else None,
        user_agent=request.headers.get("user-agent"),
        ip=_client_ip(request),
    )
    return ok(request, session_out.model_dump(by_alias=True))
