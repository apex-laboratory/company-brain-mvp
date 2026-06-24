# PR: KAN-51 - OAuth + Passwordless Auth

## What this PR does

Implements the full authentication layer for the Brainite API:

- Passwordless email signup and signin
- Google and GitHub OAuth 2.0 (authorization code flow with 6-point state validation)
- JWT access tokens (15 min TTL) with rotating refresh tokens (30 day TTL)
- Logout with session revocation
- SAML stub returning 501

## Endpoints

```
POST /api/v1/auth/signup
POST /api/v1/auth/signin
GET  /api/v1/auth/oauth/{provider}/start
POST /api/v1/auth/oauth/{provider}/callback
POST /api/v1/auth/refresh
POST /api/v1/auth/logout
```

Providers: `google`, `github`, `saml` (stub).
Rate limit: 10 req/min per IP on all auth endpoints.

## What was tested

- All endpoints verified with curl (see docs/POSTMAN_SETUP.md)
- Google OAuth end-to-end: auth URL, browser sign-in, code exchange, token response
- Email signup, signin, refresh, and logout flows
- Invalid provider (400), SAML stub (501), expired/consumed state (401)
- 45 unit tests passing

## Other changes

- `docker-compose.yml`: removed stale `brain-api` service (wrong entry point)
  and `schema.sql` mount (file does not exist)
- `brain-api/requirements.txt`: added `pytest-asyncio` (was missing, breaks CI)
- `docs/POSTMAN_SETUP.md`: rewritten with accurate startup steps and working examples

## Notes

- GitHub OAuth creds are intentionally blank in the shared env (other dev owns those)
- `workspace` returns null post-auth by design, KAN-52 handles workspace creation
