# Brainite

Brainite extracts operational knowledge from your company's tools — Slack, Jira, GitHub, Notion, Zendesk — structures it into versioned, queryable skills, and serves it to AI agents via MCP.

## Architecture

Multi-tenant SaaS. Each company gets isolated data via Postgres Row Level Security. Skills are stored as versioned records with vector embeddings for semantic search.

```
Sources (Slack / Jira / GitHub / Notion / Zendesk)
  → Webhook ingest + sweep pipeline
  → Groq 6-step extraction → skills
  → PostgreSQL (Supabase) + pgvector
  → FastMCP SSE → AI agents
```

## Stack

| Layer | Technology |
|-------|-----------|
| Database | Supabase (Postgres 16 + pgvector + RLS) |
| API | FastAPI |
| Agent delivery | FastMCP SSE |
| Vector search | HNSW index (1536-dim) |
| Token encryption | pgcrypto (BYTEA) |
| Auth | Supabase Auth (JWT with org_id claim) |

## Database schema

16 tables across three concerns:

**Tenant layer** — `organizations`, `users`, `organization_members`, `invitations`, `organization_settings`

**Knowledge layer** — `skills`, `skill_versions`, `review_queue`, `agent_interactions`

**Integration layer** — `source_connections`, `webhook_subscriptions`, `source_events`, `sweeps`

**Platform layer** — `organization_api_keys`, `organization_usage`, `audit_log`

RLS is enabled on every table. `org_id` is present on all data tables and enforced at the DB layer — no app-level filtering required. OAuth tokens and webhook secrets are stored encrypted (`BYTEA` via pgcrypto).

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
uvicorn main:app --reload --port 8000
```

## Environment variables

```
SUPABASE_URL=
SUPABASE_SERVICE_ROLE_KEY=
SUPABASE_JWT_SECRET=
ENCRYPTION_KEY=          # used by pgcrypto for OAuth token encryption
GROQ_API_KEY=
ANTHROPIC_API_KEY=
```
