# Postman Testing Guide — OAuth (KAN-51) + Slack Normalizer (L04)

---

## 1. Start the Infrastructure

From the **repo root** (where `docker-compose.yml` lives):

```bash
docker compose up -d
```

This starts:
- **PostgreSQL 16** on `localhost:5432` — db `company_brain`, user `cb`, password `cb_secret`
- **Redis 7** on `localhost:6379`

Verify they're healthy:
```bash
docker compose ps
```

---

## 2. Start the API (feat/KAN-51)

```bash
git checkout feat/KAN-51
cd brain-api
pip install -r requirements.txt        # first time only
uvicorn app.main:app --reload --port 4000
```

Confirm it's up:
```
GET http://localhost:4000/health
→ {"status": "ok"}

GET http://localhost:4000/ready
→ {"database": true, "redis": true}
```

Swagger UI is at `http://localhost:4000/docs` (dev mode only).

---

## 3. Environment Variables (brain-api/.env)

Create `brain-api/.env`:

```env
# Database — matches docker-compose defaults
DATABASE_URL=postgresql+asyncpg://cb:cb_secret@localhost:5432/company_brain

# Redis
REDIS_URL=redis://localhost:6379

# JWT — must be >= 32 characters each
JWT_ACCESS_SECRET=your-32-char-access-secret-goes-here-pad
JWT_REFRESH_SECRET=your-32-char-refresh-secret-goes-here-pad

# Encryption — exactly 64 hex characters (32 bytes)
ENCRYPTION_KEY=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef

# CORS
ALLOWED_ORIGINS=http://localhost:3000

# AI / Email (dummies work locally)
AI_SERVICE_URL=http://localhost:8000
AI_SERVICE_TOKEN=test-token-local
RESEND_API_KEY=test-key

# OAuth providers (only needed for OAuth flows)
GOOGLE_CLIENT_ID=<your-client-id>.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=<your-client-secret>
GITHUB_CLIENT_ID=<your-client-id>
GITHUB_CLIENT_SECRET=<your-client-secret>

ENVIRONMENT=development
```

> `ALLOWED_HOSTS` defaults to `*` in development — skip it locally.

---

## 4. Run Migrations

```bash
cd brain-api
alembic upgrade head
```

---

## 5. Postman Setup

1. **Environments** → New → Name: `brainite-local`
   - `base_url` = `http://localhost:4000`
   - `access_token` = *(fill after sign-in)*
   - `refresh_token` = *(fill after sign-in)*

2. For protected endpoints set **Authorization** → Bearer Token → `{{access_token}}`

---

## 6. Testing feat/KAN-51 — OAuth

All endpoints under `/api/v1/auth`. Rate limit: **10 req/min per IP**.

> **Request bodies use camelCase JSON** — `refreshToken` not `refresh_token`.

### 6a. Passwordless Email Sign-Up

```
POST {{base_url}}/api/v1/auth/signup
Content-Type: application/json

{
  "email": "test@example.com"
}
```

Sign-up is email-only (no password). Returns `201` with `accessToken`, `refreshToken`, `user`, `workspace`, `nextStep`.

### 6b. Passwordless Email Sign-In

```
POST {{base_url}}/api/v1/auth/signin
Content-Type: application/json

{
  "email": "test@example.com"
}
```

### 6c. Google OAuth — Full Flow

**Step 1 — Get the authorization URL**
```
GET {{base_url}}/api/v1/auth/oauth/google/start?mode=signin
```

Response:
```json
{
  "status": "success",
  "code": 200,
  "data": {
    "authorizationUrl": "https://accounts.google.com/o/oauth2/v2/auth?...",
    "state": "eyJ..."
  }
}
```

**Step 2 — Complete in browser**
- Paste `authorizationUrl` into a browser and sign in with Google
- Google redirects to `http://localhost:4000/api/v1/auth/oauth/google/callback?code=...&state=...`
- Copy `code` and `state` from the URL bar

**Step 3 — Exchange code for tokens**
```
POST {{base_url}}/api/v1/auth/oauth/google/callback
Content-Type: application/json

{
  "code": "<code from step 2>",
  "state": "<state from step 1>"
}
```

Response:
```json
{
  "status": "success",
  "code": 200,
  "data": {
    "accessToken": "eyJ...",
    "refreshToken": "...",
    "user": { "id": "usr_...", "email": "you@example.com", "name": "Your Name" },
    "workspace": { "id": "wrk_...", "name": "...", "slug": "your-workspace" },
    "nextStep": "dashboard"
  }
}
```

**Step 4 — Save tokens**
- Set Postman env `access_token` = the `accessToken` value
- Set Postman env `refresh_token` = the `refreshToken` value

> State tokens expire in **10 minutes** and are single-use. `invalid_oauth_state` = start over from Step 1.

### 6d. GitHub OAuth — Full Flow

Same as Google, swap `google` → `github`:

```
GET {{base_url}}/api/v1/auth/oauth/github/start?mode=signin
POST {{base_url}}/api/v1/auth/oauth/github/callback
```

### 6e. Refresh Token

```
POST {{base_url}}/api/v1/auth/refresh
Content-Type: application/json

{
  "refreshToken": "{{refresh_token}}"
}
```

Returns new `accessToken` + `refreshToken`. Update env vars.

### 6f. Logout

```
POST {{base_url}}/api/v1/auth/logout
Content-Type: application/json

{
  "refreshToken": "{{refresh_token}}"
}
```

Returns `204 No Content`.

---

## 7. Testing L04-slack-connector — Slack Normalizer

This branch has **no API endpoints**. It's a standalone CLI that reads Airbyte staging tables and writes to `raw_content`.

### Switch branch

```bash
git checkout L04-slack-connector
```

No API restart needed — this branch doesn't touch `brain-api`.

### What it does

Reads from Airbyte staging tables (supports both V1 and V2 layouts):
- `airbyte_internal.slack_raw__stream_channel_messages`
- `airbyte_internal.slack_raw__stream_threads`
- `airbyte_internal.slack_raw__stream_channels`
- `airbyte_internal.slack_raw__stream_users`

Writes canonical rows to `raw_content`:
- `source`: `"slack"`
- `source_id`: `slack_message:{channel_id}:{ts}`
- `content`: message text with `<@USERID>` mentions resolved to display names
- `metadata`: `{entity_type, channel_name, author, created_at, record_url, thread_id, ...}`

The normalizer is **idempotent** — re-running it overwrites existing rows in place.

### Run it

```bash
# From repo root (the directory containing airbyte/)
export DATABASE_URL=postgresql://cb:cb_secret@localhost:5432/company_brain
export SLACK_WORKSPACE=your-workspace-subdomain   # optional, for permalink URLs

python -m airbyte.normalize.run slack
# or pass workspace as a flag:
python -m airbyte.normalize.run slack --workspace your-workspace-subdomain
```

Output:
```
channel_messages          42
threads                    7
total                     49
```

Exit codes: `0` = rows written, `1` = zero rows (tables empty/missing), `2` = `DATABASE_URL` not set.

### Seed test data to verify it works

```sql
CREATE SCHEMA IF NOT EXISTS airbyte_internal;

CREATE TABLE IF NOT EXISTS airbyte_internal.slack_raw__stream_channel_messages (
    _airbyte_data JSONB
);

INSERT INTO airbyte_internal.slack_raw__stream_channel_messages (_airbyte_data)
VALUES ('{
  "ts": "1700000000.000100",
  "channel_id": "C01234567",
  "user": "U01234567",
  "text": "Hello world",
  "thread_ts": null,
  "subtype": null
}');
```

Then run the normalizer and check:
```sql
SELECT source, source_id, content, metadata FROM raw_content WHERE source = 'slack';
```

---

## 8. Google OAuth App Setup (one-time)

1. [Google Cloud Console](https://console.cloud.google.com) → Create project `Brainite Local`
2. APIs & Services → Enable **Google+ API** (or People API)
3. Credentials → Create OAuth 2.0 Client ID (Web application):
   - Authorized redirect URI: `http://localhost:4000/api/v1/auth/oauth/google/callback`
4. Copy Client ID + Client Secret → add to `brain-api/.env`

## GitHub OAuth App Setup (one-time)

1. GitHub → Settings → Developer settings → OAuth Apps → New OAuth App
   - Homepage URL: `http://localhost:4000`
   - Callback URL: `http://localhost:4000/api/v1/auth/oauth/github/callback`
2. Copy Client ID, generate Client Secret → add to `brain-api/.env`

---

## 9. Common Errors

| Error | Cause | Fix |
|-------|-------|-----|
| `invalid_oauth_state` | State expired (10 min TTL) or already used | Restart from `/oauth/{provider}/start` |
| `invalid_provider` | Used a provider not in `google`, `github`, `saml` | `slack` is not a valid auth provider |
| `422 Unprocessable Entity` | Wrong field names in body | Use camelCase: `refreshToken` not `refresh_token` |
| `429 Too Many Requests` | Hit 10 req/min rate limit | Wait 60s |
| DB connection error | Postgres not running | `docker compose up -d` |
| Redis connection error | Redis not running | `docker compose up -d` |
| `ValueError: must be at least 32 characters` | JWT secret too short | Pad secrets to ≥ 32 chars |
| `ValueError: must be 64 hex characters` | Bad `ENCRYPTION_KEY` | Exactly 64 hex chars, no spaces |
| Normalizer exits with code 1 | Staging tables empty or missing | Seed test data or run an Airbyte sync first |
