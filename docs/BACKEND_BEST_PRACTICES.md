# Backend Best Practices & Project Conventions

This document defines how we build the Hephaestou / Brainite backend. It is the
**target convention** — the codebase will be brought in line with it over time.

It pairs with two other documents:

- [`API_DOCUMENTATION.md`](./API_DOCUMENTATION.md) — the external HTTP contract.
- [`DB_DESIGN`](./DB_DESIGN) — schema, tenancy, and RLS.

---

## 1. Stack

| Concern                  | Choice                                    | Notes                                                                 |
| ------------------------ | ----------------------------------------- | --------------------------------------------------------------------- |
| Language                 | **Python 3.12+**                          | Strict typing enforced via mypy. No untyped modules.                  |
| Runtime                  | CPython (uvicorn + ASGI)                  | Async throughout; `asyncio` event loop.                               |
| HTTP framework           | **FastAPI**                               | Async-native, auto OpenAPI docs, Pydantic v2 validation built in.     |
| Database                 | PostgreSQL                                | Single backend-owned database. See DB doc.                            |
| ORM / query layer        | **SQLAlchemy 2.0 async**                  | Parameterized by default; RLS via session GUC helpers.                |
| Migrations               | **Alembic**                               | Custom SQL migrations for RLS policies, enums, extensions, roles.     |
| Validation               | **Pydantic v2**                           | Every request body, query param, and path param is parsed.            |
| Auth                     | **Self-issued JWT** (access + refresh)    | Backend signs and verifies its own tokens via `python-jose`.          |
| Background jobs          | **ARQ** (async Redis Queue)               | Async-native; sync, ingestion, brain builds, usage rollups.           |
| Cache / rate-limit store | Redis                                     | Shared store so limits hold across instances.                         |
| Rate limiting            | **slowapi**                               | FastAPI-compatible; Redis-backed for multi-instance limits.           |
| Logging                  | **structlog** (structured JSON)           | Async-safe, context-bound, first-class field redaction.               |
| HTTP client              | **httpx** (async)                         | All outbound calls (AI service, OAuth providers, Resend).             |
| Email                    | Resend                                    | Via `httpx` behind an integration module.                             |
| AI / knowledge           | External **AI service** over HTTP         | Backend never calls model providers directly.                         |

> **Why structlog over loguru:** structlog binds context (request_id, user_id, workspace_id)
> to a processor chain that outputs clean JSON. loguru is fine, but structlog's bound-logger
> pattern maps naturally to the per-request child-logger pattern we need.

---

## 2. Project Structure

The layout below follows the module shape in `API_DOCUMENTATION.md`. Each
**feature module** is self-contained; cross-cutting concerns live in `shared/`.

```txt
app/
  main.py                # FastAPI app: middleware chain, router mounting. No business logic.
  server.py              # Process entry: load config, connect DB/Redis, start uvicorn.

  config/
    settings.py          # Pydantic Settings: validated env vars -> frozen typed config.
    database.py          # SQLAlchemy async engine + session factory.
    redis.py             # Redis connection pool.
    ai.py                # AI-service client config (base URL, service token, timeouts).

  modules/
    auth/
      router.py          # APIRouter: paths + dependencies only.
      service.py         # Business logic. Orchestrates repos + integrations.
      repository.py      # DB access (SQLAlchemy). The only place queries live.
      schemas.py         # Pydantic request/response schemas + inferred types.
      models.py          # SQLAlchemy ORM models (optional: co-locate or put in database/).
    users/
    workspaces/
    sources/
    decisions/
    reviews/
    skills/
    brain/
    members/
    api_keys/
    usage/
    webhooks/

  integrations/
    ai.py                # Brain query, skill invoke, build orchestration.
    resend.py            # Email send.
    slack.py  notion.py  github.py  jira.py  zendesk.py   # OAuth + read-only fetch.

  database/
    models/              # SQLAlchemy ORM model classes.
    migrations/          # Alembic migration files.
      env.py
      versions/

  shared/
    middleware/
      request_context.py # requestId + contextvars-based context.
      authenticate.py    # Verify JWT or workspace API key -> auth context.
      authorize.py       # Role / scope checks (FastAPI dependencies).
      with_tenant.py     # Set RLS session GUCs; async context manager.
      rate_limit.py      # slowapi limiter factories.
      error_handler.py   # Exception handler registration.
    errors/
      app_error.py       # Base + typed subclasses (ValidationError, ForbiddenError…).
    http/
      respond.py         # success() / created() / no_content() envelope helpers.
    logger/
      __init__.py        # structlog instance + bound-logger helpers.
    constants/
    helpers/
      ids.py             # Prefixed ID generation (usr_, wrk_, …).
      crypto.py          # Encryption + hashing wrappers.

  jobs/
    worker.py            # ARQ WorkerSettings + queue definitions.
    tasks/               # Task functions (run in a separate worker process).

  tests/
```

### Layering rules (enforced in review)

```txt
router  ->  service  ->  repository  ->  SQLAlchemy
               |
               +------>  integrations (AI service, OAuth, email)
```

- **Routers** only declare paths, dependencies, and call the service. No SQL, no ORM, no provider SDKs.
- **Services** own business logic and transactions. They are framework-agnostic
  (no `Request`/`Response`). Authorization decisions and orchestration live here.
- **Repositories** are the _only_ place SQLAlchemy queries appear. One repository per
  aggregate. They never import FastAPI types.
- **Integrations** isolate every external SDK and credential. If a vendor changes,
  only its integration file changes.
- **Never skip a layer.** A router calling SQLAlchemy directly is a review block.

---

## 3. Naming Conventions

Follow `API_DOCUMENTATION.md` exactly for anything client-facing:

- **Routes:** lowercase kebab-case path segments (`/api-keys`, `/source-items`),
  plural collection nouns, version prefix `/api/v1`, verbs only for non-CRUD actions
  (`/reviews/{review_id}/resolve`).
- **JSON fields:** `camelCase` in responses (`workspaceId`, `accessToken`, `sourceProvider`).
  Use `model_config = ConfigDict(populate_by_name=True)` and `alias_generator=to_camel`
  (via `pydantic-extra-types` or a custom generator) on response schemas.
- **IDs:** opaque, prefixed, stable — `usr_`, `wrk_`, `src_`, `dec_`, `rev_`, `skl_`,
  `key_`, `inv_`, `cnv_`, `bld_`, `act_`. Generated in `shared/helpers/ids.py`. Never
  expose raw auto-increment integers.

Internal code:

- Files: `snake_case.py` (`decisions_service.py`, `decisions_repository.py`).
- Classes: `PascalCase`. Functions/vars: `snake_case`. Constants: `UPPER_SNAKE`.
- DB columns: `snake_case` (native Postgres convention; no mapping layer needed).
- Prefer named imports over star imports; one public class/function per module is fine
  but not required.

---

## 4. Configuration & Secrets

- All config flows through `config/settings.py`, which **validates environment variables
  with Pydantic Settings at startup and raises on failure** (fail fast, never boot
  half-configured).
- Import the singleton `settings` object everywhere; **never read `os.environ` outside
  `config/`.**
- `.env` is git-ignored; `.env.example` lists every key with safe placeholders.
- Secrets (`JWT_ACCESS_SECRET`, OAuth secrets, `DATABASE_URL`, encryption keys) live
  only in backend env / a secrets manager. They are **never** sent to the frontend and
  never appear in responses or logs.

```python
# config/settings.py
from functools import lru_cache
from typing import Literal
from pydantic import AnyUrl, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        frozen=True,
    )

    environment: Literal["development", "test", "production"] = "development"
    port: int = 4000
    database_url: str                    # postgresql+asyncpg://...
    redis_url: str                       # redis://...
    jwt_access_secret: str
    jwt_refresh_secret: str
    encryption_key: str                  # 64 hex chars = 32 bytes AES key
    allowed_origins: list[str]           # comma-separated in env, parsed below
    ai_service_url: str
    ai_service_token: str
    resend_api_key: str
    default_from_email: str = "Brainite <founders@brainites.com>"

    @field_validator("jwt_access_secret", "jwt_refresh_secret", mode="after")
    @classmethod
    def min_length_32(cls, v: str) -> str:
        if len(v) < 32:
            raise ValueError("must be at least 32 characters")
        return v

    @field_validator("encryption_key", mode="after")
    @classmethod
    def must_be_64_hex(cls, v: str) -> str:
        if len(v) != 64:
            raise ValueError("must be 64 hex characters (32 bytes)")
        return v

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def parse_origins(cls, v: str | list) -> list[str]:
        if isinstance(v, str):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
```

---

## 5. Request Validation

- **Every** request body, query param, and path param is declared as a Pydantic model
  or `Annotated` type in the router signature. FastAPI validates before the route
  function runs. Unvalidated input never reaches a service.
- Use `model_config = ConfigDict(extra="forbid")` on request schemas to reject
  unexpected keys (defends against mass assignment).
- Coerce/normalize at the edge (`.strip()`, `.lower()` on emails, `int` coercion via
  `Annotated[int, Query(ge=1, le=100)]`). Enforce `limit` caps from the API doc
  (default 25, max 100).
- Validation failures return `422` with the standard `validation_error` envelope.

```python
# modules/decisions/schemas.py
from pydantic import BaseModel, ConfigDict, EmailStr, field_validator
from typing import Annotated, Literal
from pydantic import Field


class DecisionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=500)
    category: str | None = None
    source_provider: Literal["slack", "notion", "github", "jira", "zendesk"] | None = None
    summary: str | None = None


class DecisionListQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["approved", "active", "review"] | None = None
    category: str | None = None
    source_provider: str | None = None
    limit: Annotated[int, Field(ge=1, le=100)] = 25
    cursor: str | None = None
```

```python
# modules/decisions/router.py
from fastapi import APIRouter, Depends
from .schemas import DecisionCreateRequest, DecisionListQuery
from .service import DecisionService
from shared.middleware.authenticate import get_auth_context
from shared.middleware.authorize import require_role

router = APIRouter(prefix="/decisions", tags=["decisions"])


@router.post("", status_code=201)
async def create_decision(
    body: DecisionCreateRequest,
    auth=Depends(get_auth_context),
    _=Depends(require_role("editor")),
    service: DecisionService = Depends(),
):
    decision = await service.create(auth, body)
    return created(decision)


@router.get("")
async def list_decisions(
    query: Annotated[DecisionListQuery, Query()],
    auth=Depends(get_auth_context),
    service: DecisionService = Depends(),
):
    return ok(await service.list(auth, query))
```

> **Injection:** SQLAlchemy parameterizes all queries — never build SQL via string
> concatenation. If `text()` is unavoidable, always use bound parameters
> (`text("WHERE id = :id").bindparams(id=value)`), never f-strings.

---

## 6. Security Middleware

The `main.py` middleware chain, in order:

1. **`CORSMiddleware`** — strict allowlist from `settings.allowed_origins`.
   Never `allow_origins=["*"]` in production. `allow_credentials=True` only for
   dashboard origins (needed for the refresh-token cookie).
2. **`TrustedHostMiddleware`** — rejects requests with unexpected `Host` headers.
3. **Request context** — assign `request_id` (`req_…`), store in `contextvars`,
   attach a bound structlog logger.
4. **Rate limiting** — `slowapi` limiter, per-surface (see §9).
5. **Routes** (each with `authenticate` → `authorize` / `with_tenant` → validated body).
6. **Exception handlers** (registered via `app.add_exception_handler`).

```python
# main.py
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from shared.middleware.request_context import RequestContextMiddleware
from shared.middleware.error_handler import register_exception_handlers
from shared.middleware.rate_limit import limiter
from config.settings import settings
from modules.auth.router import router as auth_router
# ... other routers

app = FastAPI(title="Brainite API", version="1.0.0", docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["*"])  # tighten in prod
app.add_middleware(RequestContextMiddleware)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

register_exception_handlers(app)

app.include_router(auth_router, prefix="/api/v1/auth")
# ... other routers under /api/v1
```

Additional hardening:

- Disable the FastAPI interactive docs (`docs_url=None`, `redoc_url=None`) in production.
- Validate provider **webhook signatures** before doing any work; respond fast
  (`{"data": {"received": True}}`) and enqueue a job — never process inline.
- Set `X-Request-ID` on every response from the request context middleware.

---

## 7. Authentication & Tokens

Two credential types (per the API doc):

### Dashboard JWT (self-issued)

```python
# shared/middleware/authenticate.py
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt
from config.settings import settings

bearer = HTTPBearer(auto_error=False)


async def get_auth_context(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
):
    if credentials:
        try:
            payload = jwt.decode(
                credentials.credentials,
                settings.jwt_access_secret,
                algorithms=["HS256"],
            )
            return AuthContext(
                user_id=payload["sub"],
                workspace_id=payload["workspace_id"],
                role=payload["role"],
                scopes=payload.get("scopes", []),
                kind="jwt",
            )
        except JWTError:
            pass

    # Fall through to API key check
    api_key = request.headers.get("X-API-Key")
    if api_key:
        return await resolve_api_key(api_key)

    raise UnauthorizedError()
```

- **Access token:** short-lived (~15 min), signed with `JWT_ACCESS_SECRET` using
  HS256. Sent as `Authorization: Bearer <token>`. Stateless.
- **Refresh token:** long-lived, signed with `JWT_REFRESH_SECRET`, **rotated on every
  use**, stored **hashed** (SHA-256) in the DB so it can be revoked. Delivered as an
  `httpOnly`, `secure`, `samesite=strict` cookie (never readable by JS).
- On refresh: verify → look up the hashed token → issue a new pair → invalidate the
  old one. Detect reuse → revoke the whole token family.
- The auth flow is **passwordless** (email-only), so no password column today. If
  passwords are added, hash with **argon2id** (`argon2-cffi`) — never store or log
  plaintext.

### Workspace API key (agents / MCP)

- Format `hph_live_…`. **Show the raw key once**, store only a **SHA-256 hash** plus a
  short display `prefix`. Look up by hash on each call; compare with `hmac.compare_digest`
  (timing-safe). Track `last_used_at`, `scopes`, `expires_at`, `revoked_at`.

### OAuth `state`

- Connection start generates a **signed, short-lived `state`** (HMAC-SHA256 with
  `JWT_ACCESS_SECRET`). The callback verifies: signature, expiry, current user/workspace
  ownership, requested provider, and redirect URI before exchanging the code.

### Authorization

- `get_auth_context` resolves the caller into an `AuthContext(user_id, workspace_id, role, scopes, kind)`.
- `require_role("admin")` / `require_scope("brain:query")` are FastAPI dependencies
  that enforce role (`admin | editor | viewer`) for dashboard routes and scope for
  API-key routes.
- Authorization is enforced in the **service/dependency layer** _and_ backstopped by
  RLS in the DB (defense in depth — see §8 and the DB doc).

---

## 8. Tenant Isolation & RLS from the App Layer

Every workspace-scoped query is protected at two levels:

1. **Application scoping** — repositories always filter by `workspace_id` taken from
   `AuthContext`, never from the request body. A missing tenant context is a bug, not
   a default-to-all.
2. **Database RLS (backstop)** — Postgres Row-Level Security policies reject any row
   whose workspace doesn't match the session's current workspace, so a forgotten
   `WHERE` clause cannot leak another tenant's data.

Because we issue our own JWTs (not Supabase), the backend **sets the RLS session
variable itself** inside a transaction. Use the `run_in_tenant` async context manager:

```python
# shared/middleware/with_tenant.py
from contextlib import asynccontextmanager
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text


@asynccontextmanager
async def run_in_tenant(
    session: AsyncSession,
    workspace_id: str,
    user_id: str,
    role: str,
):
    await session.execute(
        text("SELECT set_config('app.current_workspace_id', :v, true)"),
        {"v": workspace_id},
    )
    await session.execute(
        text("SELECT set_config('app.current_user_id', :v, true)"),
        {"v": user_id},
    )
    await session.execute(
        text("SELECT set_config('app.current_role', :v, true)"),
        {"v": role},
    )
    yield session
```

`set_config(..., true)` makes the setting **transaction-local**, so it cannot bleed
across pooled connections. RLS policies read `current_setting('app.current_workspace_id', true)`.
The connection role used for tenant traffic must **not** have `BYPASSRLS`. See
`DB_DESIGN` §RLS for the policy definitions.

Usage in a service:

```python
async with get_session() as session:
    async with run_in_tenant(session, auth.workspace_id, auth.user_id, auth.role):
        result = await decision_repo.list(session, workspace_id=auth.workspace_id)
```

---

## 9. Rate Limiting

Use `slowapi` with a **Redis backend** (so limits are shared across instances) and a
key function appropriate to each surface. Defaults from the API doc:

| Surface          | Limit      | Key          |
| ---------------- | ---------- | ------------ |
| Auth endpoints   | 10 / min   | IP           |
| Dashboard API    | 300 / min  | user_id      |
| Brain query      | 60 / min   | workspace_id |
| Skill invoke     | 600 / min  | workspace_id |
| OAuth callbacks  | 20 / min   | user_id      |
| API-key creation | 5 / hour   | user_id      |

```python
# shared/middleware/rate_limit.py
from slowapi import Limiter
from slowapi.util import get_remote_address
from config.redis import get_redis_url

limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=get_redis_url(),
    default_limits=["300/minute"],
)


def user_key(request: Request) -> str:
    auth = request.state.auth
    return f"user:{auth.user_id}" if auth else get_remote_address(request)


def workspace_key(request: Request) -> str:
    auth = request.state.auth
    return f"workspace:{auth.workspace_id}" if auth else get_remote_address(request)
```

```python
# applying in a router
@router.post("/query")
@limiter.limit("60/minute", key_func=workspace_key)
async def query_brain(request: Request, body: BrainQueryRequest, ...):
    ...
```

- On limit, return `429` with the `rate_limited` envelope and `meta.retryAfterSeconds`;
  set the `Retry-After` header.

---

## 10. Error Handling

- **One** centralized set of exception handlers, registered at app startup via
  `register_exception_handlers(app)`. Routers/services `raise` typed errors; they never
  format HTTP errors inline.
- A small `AppError` hierarchy carries an HTTP status and a stable machine `code`:

```python
# shared/errors/app_error.py
from dataclasses import dataclass, field
from typing import Any


class AppError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


class ValidationError(AppError):
    def __init__(self, details: Any = None) -> None:
        super().__init__(422, "validation_error", "Request is invalid.", details)


class UnauthorizedError(AppError):
    def __init__(self) -> None:
        super().__init__(401, "unauthorized", "Authentication required.")


class ForbiddenError(AppError):
    def __init__(self) -> None:
        super().__init__(403, "forbidden", "You are not allowed to do that.")


class NotFoundError(AppError):
    def __init__(self, resource: str = "Resource") -> None:
        super().__init__(404, "not_found", f"{resource} not found.")


class ConflictError(AppError):
    def __init__(self, message: str = "Conflict") -> None:
        super().__init__(409, "conflict", message)


class RateLimitError(AppError):
    def __init__(self, retry_after: int) -> None:
        super().__init__(
            429,
            "rate_limited",
            "Too many requests. Try again later.",
            {"retryAfterSeconds": retry_after},
        )
```

```python
# shared/middleware/error_handler.py
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from shared.errors.app_error import AppError
from shared.logger import get_logger

log = get_logger()


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def pydantic_validation_handler(request: Request, exc: RequestValidationError):
        details = [
            {"path": ".".join(str(p) for p in e["loc"]), "message": e["msg"]}
            for e in exc.errors()
        ]
        request_id = getattr(request.state, "request_id", None)
        return JSONResponse(
            status_code=422,
            content={
                "error": {"code": "validation_error", "message": "Request is invalid.", "details": details},
                "meta": {"requestId": request_id},
            },
        )

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError):
        request_id = getattr(request.state, "request_id", None)
        if exc.status >= 500:
            log.error("request_failed", error=str(exc), code=exc.code)
        else:
            log.warning("request_rejected", code=exc.code)
        return JSONResponse(
            status_code=exc.status,
            content={
                "error": {"code": exc.code, "message": exc.message, "details": exc.details},
                "meta": {"requestId": request_id},
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception):
        request_id = getattr(request.state, "request_id", None)
        log.exception("unhandled_error", error=str(exc))
        return JSONResponse(
            status_code=500,
            content={
                "error": {"code": "internal_error", "message": "Internal Server Error"},
                "meta": {"requestId": request_id},
            },
        )
```

- **No data leakage:** never return raw DB errors, stack traces, SQL, or provider
  payloads to the client. Map SQLAlchemy `IntegrityError` (`UniqueViolation`) →
  `ConflictError` (409), `NoResultFound` → `NotFoundError` (404).
- Add `asyncio`'s unhandled exception handler for tasks that escape the request cycle.

---

## 11. Logging

- **structlog**, JSON output, one **bound logger per request** carrying `request_id`,
  `user_id`, `workspace_id`, and (for jobs) `job_id`, `provider`.
- **Always log:** request ids, user/workspace/job ids, provider names, durations, outcome/status.
- **Never log:** access/refresh tokens, API keys, OAuth codes/secrets, encryption keys,
  raw provider secrets, full PII payloads. Configure structlog processors to redact
  fields matching `*token*`, `*api_key*`, `*password*`, `*secret*`, `authorization`,
  `cookie`.
- Use levels deliberately: `error` (500s, unexpected), `warning` (4xx client errors,
  retries), `info` (lifecycle, request summary), `debug` (dev only).

```python
# shared/logger/__init__.py
import logging
import structlog
from config.settings import settings

REDACTED_KEYS = frozenset({
    "authorization", "cookie", "token", "access_token", "refresh_token",
    "api_key", "password", "secret", "encryption_key",
})


def _redact_sensitive(logger, method, event_dict):
    for key in list(event_dict.keys()):
        if any(r in key.lower() for r in REDACTED_KEYS):
            event_dict[key] = "[REDACTED]"
    return event_dict


structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        _redact_sensitive,
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(
        logging.DEBUG if settings.environment != "production" else logging.INFO
    ),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
)


def get_logger() -> structlog.BoundLogger:
    return structlog.get_logger()
```

Bind per-request context once in the request context middleware; all subsequent log
calls in that request automatically carry it:

```python
structlog.contextvars.bind_contextvars(
    request_id=request_id,
    user_id=auth.user_id,
    workspace_id=auth.workspace_id,
)
```

---

## 12. Async & Non-Blocking Discipline

- **No synchronous I/O in request paths.** No `open()` / `os.*` sync file ops,
  `subprocess` blocking calls, or `time.sleep` — they block the event loop and enable
  DoS. Use `asyncio`, `aiofiles`, or `anyio.to_thread.run_sync` for CPU-bound work.
- Offload anything slow (sync, ingestion, extraction, embeddings, AI builds, bulk email)
  to **ARQ jobs**; HTTP handlers enqueue and return `202 Accepted` with a job/build id
  the client can poll.
- Always set **timeouts** on outbound `httpx` calls (AI service, OAuth providers, Resend).
  Use `httpx.AsyncClient(timeout=httpx.Timeout(10.0))`. Never make an external call without
  a timeout.
- Use `asyncio.gather` for independent awaits; don't serialize unrelated I/O.

```python
# correct: parallel fetches
source, members = await asyncio.gather(
    source_repo.get(session, source_id),
    member_repo.list(session, workspace_id),
)

# wrong: unnecessary serialization
source = await source_repo.get(session, source_id)
members = await member_repo.list(session, workspace_id)
```

---

## 13. Background Jobs

- Job definitions in `jobs/tasks/`; ARQ `WorkerSettings` in `jobs/worker.py`. Workers
  run in a **separate process** from the API (`arq app.jobs.worker.WorkerSettings`).
- Jobs from the API doc: `source_sync`, `brain_build`, `review_generate`,
  `skill_generate`, `usage_rollup`. Each job is **idempotent** (dedupe on a stable
  external id) and has retry/backoff configured in `WorkerSettings`.
- Job context carries `workspace_id` and runs its DB work through the same `run_in_tenant`
  context manager so RLS applies to workers too.

```python
# jobs/worker.py
from arq import cron
from arq.connections import RedisSettings
from config.settings import settings


async def source_sync(ctx: dict, workspace_id: str, source_id: str) -> None:
    async with get_session() as session:
        async with run_in_tenant(session, workspace_id, "system", "admin"):
            await _do_sync(session, source_id)


class WorkerSettings:
    functions = [source_sync, brain_build, review_generate, skill_generate, usage_rollup]
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    max_jobs = 10
    job_timeout = 600
    retry_jobs = True
    max_tries = 3
```

---

## 14. Responses

- One envelope for everything, via `shared/http/respond.py`:
  - Success: `{ "data": ..., "meta": { "requestId": ..., "timestamp": ... } }`.
  - Lists: include `meta.nextCursor` (cursor pagination, never offset for large sets).
  - Errors: `{ "error": { "code": ..., "message": ..., "details": ... }, "meta": { "requestId": ... } }`.
- Use the correct status codes from the API doc (`201` create, `202` queued,
  `204` no body, `409` conflict, `422` validation, `429` limited).

```python
# shared/http/respond.py
from fastapi import Request
from fastapi.responses import JSONResponse
from datetime import datetime, timezone


def _meta(request: Request, extra: dict = {}) -> dict:
    return {
        "requestId": getattr(request.state, "request_id", None),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **extra,
    }


def ok(request: Request, data, **meta) -> JSONResponse:
    return JSONResponse({"data": data, "meta": _meta(request, meta)})


def created(request: Request, data) -> JSONResponse:
    return JSONResponse({"data": data, "meta": _meta(request)}, status_code=201)


def accepted(request: Request, data) -> JSONResponse:
    return JSONResponse({"data": data, "meta": _meta(request)}, status_code=202)


def no_content() -> JSONResponse:
    return JSONResponse(None, status_code=204)
```

---

## 15. Testing

- **Unit:** services with repositories/integrations mocked (business rules,
  authorization decisions, edge cases). Use `pytest` + `pytest-asyncio` + `unittest.mock`.
- **Integration:** routes against a real Postgres (via `testcontainers-python` or a
  disposable schema) with **RLS enabled**, asserting cross-tenant isolation explicitly
  ("workspace A cannot read workspace B's data").
- **Contract:** responses match `API_DOCUMENTATION.md` (envelope shape, codes,
  field names). Use `httpx.AsyncClient(app=app, base_url="http://test")` for async
  test clients against the FastAPI app.
- Validate the unhappy paths: 401/403/404/409/422/429.

```python
# tests/conftest.py
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from app.main import app

@pytest_asyncio.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
```

---

## 16. Dependency & Operational Hygiene

- Run `pip-audit` (and Dependabot) regularly; treat high/critical CVEs as release blockers.
- Pin dependencies in `requirements.txt` or `pyproject.toml`; commit the lockfile
  (`pip-tools` generates `requirements.txt` from `requirements.in`).
- `GET /health` (liveness) and `GET /ready` (DB + Redis reachable) endpoints for orchestration.
- **Graceful shutdown:** uvicorn handles `SIGTERM`; the FastAPI lifespan handler closes
  the SQLAlchemy engine, Redis pool, and ARQ queues cleanly.

```python
# main.py (lifespan)
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db_pool()
    await init_redis()
    yield
    await close_db_pool()
    await close_redis()

app = FastAPI(lifespan=lifespan)
```

- Keep AI prompts in `modules/ai/prompts/` (or in the AI service); they are versioned,
  not inlined ad hoc.
- Type-check with `mypy --strict` in CI; lint with `ruff`. Both must pass before merge.

---

## 17. Review Checklist

Before a PR merges:

- [ ] All new code is typed; passes `mypy --strict` and `ruff`.
- [ ] Router has no SQLAlchemy / ORM / provider SDK calls.
- [ ] Repository is the only place queries live; all are `workspace_id`-scoped.
- [ ] Request body/query/params declared with `extra="forbid"` Pydantic schemas.
- [ ] Errors are raised as typed `AppError` subclasses; nothing internal leaks to the client.
- [ ] No secrets, tokens, or PII in logs (structlog redactor covers new field names).
- [ ] No blocking I/O in async path; all outbound `httpx` calls have explicit timeouts.
- [ ] Tenant-scoped tables exercised under RLS; cross-tenant test added if relevant.
- [ ] Rate limiter applied to any new sensitive endpoint.
- [ ] Response matches the envelope and status codes in `API_DOCUMENTATION.md`.
