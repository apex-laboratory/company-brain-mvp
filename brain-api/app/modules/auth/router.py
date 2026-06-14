"""Auth router.

Mounted under ``/api/v1/auth`` by ``main.py``. Owns the passwordless email
endpoints (KAN-49); the remaining Auth/Onboarding routes (OAuth, refresh, logout)
are added by sibling endpoint tickets.

Both routes are rate limited to ``AUTH_LIMIT`` (10/min) keyed on client IP — the
limiter's default ``key_func`` is ``get_remote_address`` — to blunt email
enumeration and token-minting abuse before sign-in exists.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.modules.auth.schemas import EmailSigninRequest, EmailSignupRequest
from app.modules.auth.service import AuthService
from app.shared.http.respond import created, ok
from app.shared.middleware.rate_limit import AUTH_LIMIT, limiter

router = APIRouter(prefix="/auth", tags=["auth"])


def get_auth_service() -> AuthService:
    return AuthService()


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client is not None else None


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
