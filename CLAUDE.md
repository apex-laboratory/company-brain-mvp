# CLAUDE.md — Company Brain (Brainite) backend

Orientation for anyone (human or AI) working in this repo. Keep it short; link out
for detail. **This repo is backend-only** — the web UI (onboarding, review-queue
cards) lives in a **separate frontend repo**, so judge features here by their API
surface, not by templates.

## What this is

Extracts operational knowledge from company tools (Slack, Notion, GitHub, Jira,
Zendesk, Google Drive/Gmail) into versioned, human-reviewed **skills**, and serves
them to AI agents via a `query_brain` MCP tool + REST. Multi-tenant; every company
is isolated by Postgres Row-Level Security. Full product spec: `docs/PRD.md`.

## Current status (keep this section updated)

| Phase | What | Status |
|-------|------|--------|
| 1 Infrastructure | compose, schema, RLS, app scaffold | ✅ merged to `main` |
| 2 Sources + onboarding | connectors, OAuth, sweep, webhooks | ✅ merged to `main` |
| 3 Extraction engine | relevance→2-pass→boundary→contradiction→confidence→embed→write, eval harness | ✅ merged to `main` (`app/pipeline/`) |
| 4 Review system (backend) | list/stats/approve/reject + get-by-id/write/resolve/bulk-approve | 🔵 in PR (branch `feature/phase-4-5-delivery`) |
| 5 Delivery | `query_brain` MCP tool, `/skills/*`, `/interactions/override`, read-cache | 🔵 in PR (same branch) |
| — Query-driven live search (Feature 16) | per-source `search()` behind the seam in `app/pipeline/query_extraction.py` | ⏳ **remaining** — see PRD §16 Phase 5 note |
| 6 Agent demo | `agent-demo/demo.py` | ⏳ placeholder |

The review UI and onboarding UI are the frontend repo's job, not ours.

## Repo layout

- **`brain-api/app/`** — the **only** canonical application tree. Everything lives here.
  - `app/main.py` — FastAPI app (run: `uvicorn app.main:app`). `app/mcp/server.py` — MCP `query_brain` (own process, `python -m app.mcp.server`). `app/jobs/worker.py` — ARQ worker (`arq app.jobs.worker.WorkerSettings`).
  - `app/modules/<name>/{router,service,repository,schemas}.py` — one folder per feature. `app/pipeline/` — extraction engine. `app/integrations/` — connectors. `app/shared/` — auth, RLS, errors, http envelope, rate limit, crypto.
- **`brain-api/config.py` + `brain-api/models/orm/`** — kept only because **alembic** (`alembic/env.py`) imports them for migration metadata. The old top-level `main.py`/`routers/`/`services/`/`mcp_server/` scaffold was deleted; do not resurrect it.
- **`docs/`** — `PRD.md` (product spec), `BACKEND_BEST_PRACTICES.md` (the authoritative conventions doc — read it), `API_DOCUMENTATION.md`, `api.html` + `openapi.json` (generated), `INTEGRATIONS.md`.

## Non-negotiable conventions (full detail: `docs/BACKEND_BEST_PRACTICES.md`)

Every new endpoint or agent-facing tool MUST:

1. **Resolve identity** via `get_auth_context` (`app/shared/middleware/authenticate.py`) — JWT (dashboard) or `X-API-Key` (agents → `viewer` role + scopes).
2. **Scope every DB access** inside `tenant_session(auth, workspace_id)` / `run_in_tenant` (`app/shared/middleware/with_tenant.py`) so Postgres RLS applies. Guard cross-workspace access with `assert_workspace_member`.
3. **Authorize** with `require_role` (JWT hierarchy), `require_scope` (API-key scopes), or `require_brain_access(scope)` (agent read surface — fails closed by credential kind).
4. **Keep SQL parameter-bound** — never string-interpolate. Repositories are stateless, take `session` first, and are the only place SQL lives.
5. **Return** via the `ok()/created()` envelope (`app/shared/http/respond.py`); raise typed `AppError` subclasses (never bare `HTTPException` for domain errors).
6. **Rate-limit** internet-facing routes with `@limiter.limit(...)`.
7. **Do network I/O (LLM/embedding calls) OUTSIDE any open transaction** — never pin a pooled connection during an OpenAI/Groq call. See the two-phase pattern in `app/modules/reviews/service.py::approve`.
8. **Module shape**: `router` (paths + deps only) → `service` (business logic) → `repository` (SQL). Response schemas serialize camelCase (`CamelModel`); request schemas reject unknown keys (`CamelRequestModel`).

### Auth model in one line

`Authorization: Bearer <jwt>` for dashboard users (roles: viewer<editor<admin) · `X-API-Key: <key>` for agents (role `viewer` + scopes: `brain:query`, `skills:invoke`, …). RLS backstops everything.

## Commands

```bash
make docs        # regenerate docs/openapi.json + docs/api.html from the app
make test        # backend test suite
make help        # list targets

# Tests: canonical command is app/tests only.
cd brain-api && .venv/bin/pytest app/tests -q

# Migrations (needs brain-api/config.py + models/orm — do not delete those):
cd brain-api && alembic upgrade head

# Full stack: docker compose up  (postgres, redis, api:8000, mcp:8001, worker)
```

## Testing conventions

- Unit-test services by injecting **mocked repositories** (see `app/tests/reviews/test_reviews.py`, `app/tests/skills/test_skills.py`): patch `get_session`/`run_in_tenant`/`cache`/`embedder`, assert the SQL-free logic.
- Router tests drive the ASGI app with `get_auth_context` overridden and the limiter disabled.
- Always add **cross-tenant negative tests** and **auth-failure tests** (missing/invalid key → 401, missing scope/role → 403) for new endpoints.
- Integration tests that need a real DB skip when the stack is absent (`scripts/provision_test_db.py` provisions one).

## Gotchas

- **Two app trees are gone** — `app/` is canonical. If you see imports of top-level `routers`/`services`/`mcp_server`, that's stale.
- **`config.py` + `models/orm/` are alembic-only**; the runtime uses `app/config/settings.py`. Don't wire the runtime to the legacy config.
- **`query_brain` runs as its own process** (port 8001), authenticated by `X-API-Key` + `brain:query` scope — not inside the API lifespan.
- **Query-driven extraction** (`app/pipeline/query_extraction.py`) is contract-complete but `search_sources` returns `[]` until per-provider search is built — an honest no-match, never a fabricated skill. Build order is in PRD §16 Phase 5.
- After changing any route/schema, run **`make docs`** and commit the regenerated `docs/api.html` + `docs/openapi.json`.

## Git / PR

- Branch off `main`; never commit straight to it. Conventional-commit messages.
- Run `make test` before pushing. New endpoints ship with tests in the same PR.
