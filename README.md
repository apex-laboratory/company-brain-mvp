# Brainite

Brainite extracts operational knowledge from your company's tools — Slack, Jira, GitHub, Notion, Zendesk — structures it into versioned, queryable skills, and serves it to AI agents via MCP.

## Architecture

Multi-tenant SaaS. Each company gets isolated data via Postgres Row Level Security. Skills are stored as versioned records with vector embeddings for semantic search.

```
Sources (Slack / Jira / GitHub / Notion / Zendesk)
  → Webhook ingest + sweep pipeline
  → Gemini 6-step extraction → skills
  → PostgreSQL (Supabase) + pgvector
  → FastMCP SSE → AI agents
```

## Stack

| Layer            | Technology                                  |
| ---------------- | ------------------------------------------- |
| Database         | Supabase (Postgres 16 + pgvector + RLS)     |
| API              | FastAPI                                     |
| Agent delivery   | FastMCP SSE                                 |
| Vector search    | HNSW index (1536-dim)                       |
| Token encryption | pgcrypto (BYTEA)                            |
| Auth             | Supabase Auth (JWT with workspace_id claim) |

## Database schema

**Tenant layer** — `workspaces`, `users`, `workspace_members`, `invitations`, `workspace_settings`

**Knowledge layer** — `skills`, `skill_versions`, `reviews`, `agent_interactions`

**Integration layer** — `source_connections`, `source_channels`, `webhook_subscriptions`, `source_events`, `sweeps`

**Platform layer** — `api_keys`, `usage_periods`, `audit_log`

RLS is enabled on every workspace-scoped table. `workspace_id` is present on all data tables and enforced at the DB layer — no app-level filtering required. `companies` is the one table with no `workspace_id`; it's a cross-tenant registry, so RLS does not apply. OAuth tokens and webhook secrets are stored encrypted (`BYTEA` via pgcrypto).

See `docs/schema-future-improvements.md` for deferred architectural decisions.

## RBAC

```
owner > admin > editor > viewer
```

- `owner / admin` — manage members, OAuth connections, API keys, settings
- `editor` — create and publish skills, resolve review queue
- `viewer` — read-only access to skills and agent interactions

## Local development

```bash
# API
cd brain-api
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload   # http://localhost:4000

# Worker (ARQ) — processes source syncs, extraction jobs, embedding backfills, etc.
# Requires Redis (REDIS_URL) and runs in its own process, separate from the API.
cd brain-api
arq app.jobs.worker.WorkerSettings

# MCP server (query_brain) — its own process, separate from the API.
cd brain-api
python -m app.mcp.server   # http://localhost:8001

# Full stack (postgres, redis, api:8000, mcp:8001, worker)
docker compose up
```

## Environment variables

```
SUPABASE_URL=
SUPABASE_SERVICE_ROLE_KEY=
SUPABASE_JWT_SECRET=
ENCRYPTION_KEY=          # used by pgcrypto for OAuth token encryption
GEMINI_API_KEY=
ANTHROPIC_API_KEY=
REDIS_URL=               # required by the worker (arq job queue)
```
