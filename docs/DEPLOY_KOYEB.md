# Deploying to Koyeb

How to run `api`, `mcp`, and `worker` as three services inside **one Koyeb app**,
sharing the single `brain-api/Dockerfile` image — the same shape as
`docker-compose.yml`, minus the containers Koyeb doesn't host for you.

## How it works

```
docker-compose.yml (local)          Koyeb app "company-brain" (deployed)
  postgres  ──────────────────▶     Supabase (already external — no change)
  redis     ──────────────────▶     Upstash Redis (Koyeb has no managed Redis)
  api       ──────────────────▶     service "api"    (web,    port 8000)
  mcp       ──────────────────▶     service "mcp"     (web,    port 8001)
  worker    ──────────────────▶     service "worker"  (worker, no port)
```

All three services build from `brain-api/Dockerfile` with the working
directory set to `brain-api/` (the Dockerfile does `COPY requirements.txt .`
then `COPY . .`, so the build context has to be that subdirectory, not the
repo root). Only the run command differs per service — same pattern the
`command:` overrides in `docker-compose.yml` already use.

Koyeb also offers a docker-compose *import* (wraps `docker compose up` inside
a single container via the `koyeb/docker-compose` base image). Don't use it
here — it collapses api/mcp/worker back into one container and can't express
`worker` as a no-public-port background service, which is the whole point of
splitting them.

## Prerequisites

- Koyeb account + [CLI installed](https://www.koyeb.com/docs/build-and-deploy/cli/installation) and authenticated (`koyeb login`), if deploying from the terminal instead of the dashboard
- A managed Redis instance (e.g. [Upstash](https://upstash.com)) — `REDIS_URL` in `.env.example` currently points at `localhost:6379`, which only exists in the Docker Compose network
- Existing Supabase project credentials (`DATABASE_URL`, `TENANT_DATABASE_URL`, `SUPABASE_*`) — no changes needed here, Postgres is already external

## Service topology

| Service  | Type       | Command override                          | Port | Route     | Health check |
|----------|------------|--------------------------------------------|------|-----------|--------------|
| `api`    | Web        | *(none — image default)*                   | 8000 | `/` → 8000 | `GET /health` |
| `mcp`    | Web        | `python -m app.mcp.server`                 | 8001 | `/` → 8001 | — |
| `worker` | **Worker** | `arq app.jobs.worker.WorkerSettings`       | none | none      | — |

The `worker` type is a real Koyeb concept (no public port, no route) — it
maps directly onto the ARQ process that currently has no `ports:` entry in
`docker-compose.yml`.

## Deploy via dashboard (first pass — recommended)

1. **New App** → connect the `company-brain-mvp` GitHub repo.
2. Create the first service (`api`):
   - Builder: Dockerfile
   - Working directory: `brain-api`
   - Dockerfile path: `Dockerfile` (relative to the working directory above)
   - Run command: leave blank (uses the image's default `CMD`)
   - Port: `8000`, exposed via a route on `/`
   - Health check: `GET /health`
3. Add a second service to the **same app** (`mcp`):
   - Same repo/working directory/Dockerfile as above
   - Run command: `python -m app.mcp.server`
   - Port: `8001`, route `/`
4. Add a third service (`worker`):
   - Same repo/working directory/Dockerfile
   - Service type: **Worker**
   - Run command: `arq app.jobs.worker.WorkerSettings`
   - No port, no route
5. Set environment variables on each service (see below).

## Deploy via CLI

Flag names below (`--docker-dockerfile`, `--docker-command`, `--type`,
`--ports`, `--routes`) come from `koyeb apps init --help` / `koyeb services
create --help` — confirm against your installed CLI version before running,
since git-based deploy flags have shifted between releases.

```bash
koyeb apps init company-brain \
  --git github.com/<org>/company-brain-mvp --git-branch main \
  --docker-dockerfile brain-api/Dockerfile \
  --type web --ports 8000:http --routes /:8000

koyeb services create mcp -a company-brain \
  --git github.com/<org>/company-brain-mvp --git-branch main \
  --docker-dockerfile brain-api/Dockerfile \
  --docker-command "python -m app.mcp.server" \
  --type web --ports 8001:http --routes /:8001

koyeb services create worker -a company-brain \
  --git github.com/<org>/company-brain-mvp --git-branch main \
  --docker-dockerfile brain-api/Dockerfile \
  --docker-command "arq app.jobs.worker.WorkerSettings" \
  --type worker
```

Redeploys happen automatically on push to `main` (or via `koyeb services
redeploy <app>/<service>`).

## Environment variables

Set on **all three services** (`api`, `mcp`, `worker` all touch the DB and/or
job queue) — see `.env.example` for the full annotated list:

```
DATABASE_URL
TENANT_DATABASE_URL
SUPABASE_URL
SUPABASE_SERVICE_ROLE_KEY
SUPABASE_ANON_KEY
SUPABASE_JWT_SECRET
ENCRYPTION_KEY
REDIS_URL              # → Upstash, not localhost
GEMINI_API_KEY / ANTHROPIC_API_KEY / OPENAI_API_KEY   # per LLM_PROVIDER
JWT_ACCESS_SECRET
JWT_REFRESH_SECRET
ENVIRONMENT=production # → NOT optional; see the callout below
```

`worker`-specific (source connectors it syncs on schedule):

```
GITHUB_APP_ID / GITHUB_APP_PRIVATE_KEY / GITHUB_APP_SLUG / GITHUB_WEBHOOK_SECRET
GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / GOOGLE_PUBSUB_TOPIC / GOOGLE_PUBSUB_VERIFICATION_TOKEN
NOTION_CLIENT_ID / NOTION_CLIENT_SECRET
SOURCE_AUTHORITY_PATH
```

**Update these once Koyeb assigns public URLs** — they're `localhost`
defaults in `.env.example` and will break OAuth callbacks / cross-service
calls if left as-is:

```
OAUTH_REDIRECT_BASE_URL   # → https://<api-service>.koyeb.app
AI_SERVICE_URL            # → wherever the frontend should reach `api`
MCP_BASE_DOMAIN           # → the mcp service's public domain
ALLOWED_ORIGINS           # → the frontend repo's deployed origin
FRONTEND_URL / APP_BASE_URL
```

**`ENVIRONMENT` matters beyond labeling**: on `api` it picks the refresh-token
cookie's `SameSite`/`Secure` attributes (`app/modules/auth/router.py`) —
`strict`/insecure unless `production`. FE and API are on different domains in
any real (or local-against-hosted) deployment, so `strict` gets silently
dropped by the browser on every cross-site request, breaking `POST
/auth/refresh` entirely. Set `ENVIRONMENT=production` on `api` regardless of
whether the deploy is "real" prod.

**Local FE dev against this hosted `api`**: the source-connector OAuth
callback always lands on `FRONTEND_URL` (a single static value), so if that's
the deployed frontend's origin, connecting a source bounces you there instead
of back to `localhost`. Set `FRONTEND_URLS` (plural, comma-separated) to add
origins — the callback matches the `/authorize` request's `Origin` header
against it, e.g. `FRONTEND_URLS=http://localhost:5173,https://<deployed-fe>`.

## Notes

- The `mcp` service is exposed as `web` (not `worker`) because it serves its
  own HTTP/SSE endpoint on 8001, same as in `docker-compose.yml` — agents
  authenticate to it directly via `X-API-Key`, it isn't reached through `api`.
- Postgres RLS is unaffected by any of this — `TENANT_DATABASE_URL` still
  goes through the restricted `brain_app` pool regardless of where the
  process runs.
