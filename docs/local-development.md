# Local Development Guide

All schema changes must be tested locally before touching the production Supabase instance.

## How it works

```
Local Supabase (Docker)          Production Supabase
  └── postgres:54322    ──test──▶  zimxokenvfsovnmnyesf.supabase.co
  └── studio:54323
  └── auth:54321
  └── email:54324 (Inbucket)
```

Alembic manages migrations in both environments. The local stack is a full Supabase clone running in Docker — same Postgres version, same auth, same RLS behaviour.

---

## Prerequisites

```bash
# Supabase CLI (already installed)
brew install supabase/tap/supabase

# Docker Desktop — must be running before supabase start
# https://www.docker.com/products/docker-desktop/
```

---

## One-time setup

### 1. Link to the remote project

```bash
supabase login
supabase link --project-ref zimxokenvfsovnmnyesf
```

This stores the remote connection so `supabase db diff` can compare local vs production.

### 2. Copy local env

```bash
cp .env.local.example brain-api/.env.local
# Fill in your AI API keys (ANTHROPIC_API_KEY, OPENAI_API_KEY, GROQ_API_KEY)
```

### 3. Start local Supabase

```bash
supabase start
```

Docker pulls ~1.5 GB of images on first run. Subsequent starts take ~10 seconds.

Output will show all local endpoints:
```
API URL:     http://127.0.0.1:54321
DB URL:      postgresql://postgres:postgres@127.0.0.1:54322/postgres
Studio URL:  http://127.0.0.1:54323
Inbucket:    http://127.0.0.1:54324   ← catches all emails locally
```

### 4. Apply migrations to local DB

```bash
cd brain-api
source .venv/bin/activate
DATABASE_URL=postgresql+asyncpg://postgres:postgres@127.0.0.1:54322/postgres \
  alembic upgrade head
```

Your local database now has the full schema including RLS, functions, and all 16 tables.

---

## Daily workflow

### Start the local stack

```bash
supabase start          # idempotent — safe to run if already running
supabase status         # check ports and confirm everything is up
```

### Make a schema change

1. Edit the relevant ORM model in `brain-api/models/orm/`
2. Generate a migration:
   ```bash
   cd brain-api
   alembic revision --autogenerate -m "short description of change"
   ```
3. Review the generated file in `brain-api/alembic/versions/` — Alembic autogenerate misses:
   - RLS policy changes (add as `op.execute()` manually)
   - HNSW/custom index types (add as `op.execute()` manually)
   - Postgres function changes (add as `op.execute()` manually)
4. Apply to local:
   ```bash
   alembic upgrade head
   ```
5. Verify in local Studio: http://127.0.0.1:54323
6. Run tests against local DB

### Verify diff against production (optional but recommended)

```bash
# Shows SQL diff between local schema and remote production
supabase db diff --linked
```

Use this to double-check your migration captures everything before promoting.

### Promote to production

Once the change is tested locally and the PR is reviewed:

```bash
# Point Alembic at production and upgrade
DATABASE_URL=postgresql+asyncpg://postgres:[password]@db.zimxokenvfsovnmnyesf.supabase.co:5432/postgres \
  alembic upgrade head
```

Or set your production `DATABASE_URL` in the shell environment and run `alembic upgrade head`.

---

## Reset local database

Wipes the local DB and re-applies all migrations + seed data from scratch:

```bash
supabase db reset
# Then re-apply Alembic migrations
DATABASE_URL=postgresql+asyncpg://postgres:postgres@127.0.0.1:54322/postgres \
  alembic upgrade head
```

---

## Stop local Supabase

```bash
supabase stop           # stops containers, preserves data
supabase stop --no-backup  # stops and wipes volumes (full reset next start)
```

---

## Local service URLs

| Service | URL | Notes |
|---------|-----|-------|
| Supabase Studio | http://127.0.0.1:54323 | Table editor, SQL runner, RLS policy viewer |
| REST API | http://127.0.0.1:54321 | Same as production API |
| Postgres | postgresql://postgres:postgres@127.0.0.1:54322/postgres | Direct DB access |
| Inbucket (email) | http://127.0.0.1:54324 | Catches all outgoing emails — invitation links appear here |
| Analytics | http://127.0.0.1:54327 | |

---

## Alembic quick reference

```bash
cd brain-api && source .venv/bin/activate

# Show current migration state
alembic current

# Show migration history
alembic history --verbose

# Apply all pending migrations
alembic upgrade head

# Roll back one migration
alembic downgrade -1

# Roll back to a specific revision
alembic downgrade 0001

# Generate migration from model changes
alembic revision --autogenerate -m "description"

# Mark existing DB as up-to-date without running migrations (use on fresh prod DB that already has schema)
alembic stamp head
```

---

## Environment files

| File | Purpose |
|------|---------|
| `.env.local.example` | Template for local development — copy to `brain-api/.env.local` |
| `.env.example` | Template for production — copy to `brain-api/.env` |
| `brain-api/.env.local` | Local secrets — gitignored |
| `brain-api/.env` | Production secrets — gitignored |
