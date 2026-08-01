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
from app.modules.auth.service import OAUTH_PROVIDERS, AuthService
from app.shared.errors.app_error import UnauthorizedError
from app.shared.http.respond import created, error_response, no_content, ok
from app.shared.logger import get_logger
from app.shared.middleware.authenticate import AuthContext, _from_jwt
from app.shared.middleware.rate_limit import AUTH_LIMIT, OAUTH_CALLBACK_LIMIT, limiter, user_key

log = get_logger()

router = APIRouter(prefix="/auth", tags=["auth"])

# OAuth providers wired into the code-exchange flow, plus SAML (accepted here
# but answered 501 by the service until it is implemented). Derived from the
# service registry so the two never drift.
_VALID_PROVIDERS = OAUTH_PROVIDERS | {"saml"}
_bearer = HTTPBearer(auto_error=False)


def _optional_auth(
    credentials: HTTPAuthorizationCredentials | None,
) -> AuthContext | None:
    """Resolve an optional Bearer token for the account-linking flow.

    Returns ``None`` when no token is supplied (anonymous sign-up/sign-in). A
    token that is *present but invalid/expired* is rejected with a 401 rather
    than silently downgraded to anonymous — otherwise an expired session would
    drop the intended account link without any signal to the caller.
    """
    if credentials is None:
        return None
    auth = _from_jwt(credentials.credentials)
    if auth is None:
        raise UnauthorizedError("Invalid or expired access token")
    return auth

# The raw refresh token is delivered as an httpOnly, secure cookie
# (BACKEND_BEST_PRACTICES.md §7) so it is never readable by JS. Scoped to the auth
# path so it is only sent where it is needed.
#
# SameSite is environment-driven: `strict` in dev (FE and BE share the localhost
# site) is the safest default, but a production deployment with the FE and BE on
# *different registrable domains* needs `none` (with Secure) or the browser drops
# the cookie on the cross-site refresh call. If FE and BE are same-site
# subdomains in prod, `strict` still works — override via topology if so.
#
# Secure is environment-driven too: dev runs over plain http://localhost, and a
# `Secure` cookie is silently refused by the browser on a non-HTTPS origin — it
# never gets stored, so any full-page reload (e.g. the OAuth-connect redirect
# round trip) loses the session with no error until the dead refresh call 401s.
_REFRESH_COOKIE = "refresh_token"
_REFRESH_COOKIE_PATH = "/api/v1/auth"
_REFRESH_COOKIE_SAMESITE: Literal["strict", "none"] = (
    "none" if settings.environment == "production" else "strict"
)
_REFRESH_COOKIE_SECURE: bool = settings.environment == "production"


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
        secure=_REFRESH_COOKIE_SECURE,
        samesite=_REFRESH_COOKIE_SAMESITE,
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
    response = created(request, session.model_dump(by_alias=True))
    _set_refresh_cookie(response, session.refresh_token)
    return response


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
    response = ok(request, session.model_dump(by_alias=True))
    _set_refresh_cookie(response, session.refresh_token)
    return response


@router.get("/me")
@limiter.limit(AUTH_LIMIT)
async def me(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    service: AuthService = Depends(get_auth_service),
) -> Response:
    """Return the caller's user + workspace + role from the access token.

    Requires a valid dashboard JWT (not an API key) — it is the "who am I" the FE
    calls on reload to rehydrate session state instead of trusting localStorage.
    """
    auth = _optional_auth(credentials)
    if auth is None:
        return error_response(
            request, status=401, code="unauthorized", message="Authentication required.",
        )
    result = await service.me(user_id=auth.user_id)
    return ok(request, result.model_dump(by_alias=True))


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

    auth = _optional_auth(credentials)
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

    auth = _optional_auth(credentials)
    session_out = await service.handle_oauth_callback(
        provider=provider,
        code=body.code,
        state=body.state,
        current_user_id=auth.user_id if auth else None,
        user_agent=request.headers.get("user-agent"),
        ip=_client_ip(request),
    )
    response = ok(request, session_out.model_dump(by_alias=True))
    _set_refresh_cookie(response, session_out.refresh_token)
    return response
