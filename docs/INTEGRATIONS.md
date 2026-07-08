# Integrations API — Frontend Contract

The endpoints the dashboard frontend needs to authenticate, create a workspace,
connect a source, pick what to ingest, drive the onboarding sweep, and render the
dashboard / settings screens. Everything here is **implemented** in
`brain-api/app/modules/*` and mounted under `/api/v1` by `app/main.py` — this is
the real wire contract, not the aspirational shapes in `API_DOCUMENTATION.md` (or
the stale gap list in `MISSING_APIS.md`, which predates these modules).

Frontend-facing modules that have **shipped**:

- **Auth** (`/api/v1/auth`) — passwordless email sign-up/sign-in, OAuth SSO,
  refresh-token rotation, logout.
- **Workspaces** (`/api/v1/workspaces`) — create a workspace, save onboarding
  progress, read/update settings.
- **Sources** (`/api/v1/sources`) — connect / list / disconnect providers and
  choose channels + lookback window.
- **Sweeps** (`/api/v1/sweeps`) — start and poll the onboarding backfill.
- **Dashboard** (`/api/v1/workspaces/{id}/overview` + `/activity`) — read-only
  home-screen aggregation.
- **Members** (`/api/v1/workspaces/{id}/members`) — roster + invite.
- **API keys** (`/api/v1/workspaces/{id}/api-keys`) — agent/MCP credentials.

**Webhooks** (`/api/v1/webhooks/{provider}`) also exist but are **not**
frontend-facing — they are provider→server callbacks authenticated by signature,
not the dashboard JWT. They are listed at the end for completeness only.

> **Still missing** (see `MISSING_APIS.md`): Brain chat, Decisions, Reviews,
> Skills, Usage, and a standalone Settings module. Dashboard `overview` already
> embeds decision/review *previews*, but their full list/detail/mutation routes
> are not built yet.

---

## Conventions

### Base URL

```txt
Local:      http://localhost:4000/api/v1
Production: https://api.brainite.com/api/v1
```

### Auth

Most routes require a dashboard JWT in the `Authorization` header:

```http
Authorization: Bearer <accessToken>
```

Required auth level varies by surface:

| Surface | Auth |
| --- | --- |
| `POST /auth/*` (signup, signin, refresh, logout, OAuth) | **none** — these mint or rotate the token |
| `POST /workspaces` (create) | any valid JWT (the signup token, `workspaceId=null`) |
| Sources, Sweeps, workspace **onboarding**, **settings PATCH**, **API keys** | **admin** — enforced by `require_role` + RLS; a non-admin gets `403` |
| Dashboard (`overview`, `activity`), **settings GET**, **members list** | any workspace **member** |

A workspace-scoped route additionally checks the JWT's `workspaceId` against the
`{workspace_id}` path segment — a token for workspace A cannot touch workspace B
(`403`) even if the role matches. The OAuth **callbacks** carry no JWT (browser
redirects from the provider); they authenticate via the signed, single-use
`state` instead.

### Response envelope

Success responses are wrapped:

```json
{
  "data": <payload>,
  "meta": { "requestId": "…", "timestamp": "2026-07-05T10:22:31.000Z" }
}
```

Error responses:

```json
{
  "error": { "code": "validation_error", "message": "…", "details": null },
  "meta": { "requestId": "…" }
}
```

Status codes used here: `200` ok, `202` accepted (queued), `204` no content,
`401` bad/expired OAuth state, `403` not admin, `404` unknown source/sweep,
`422` invalid body, `429` rate limited.

### ⚠ Field-casing asymmetry (known issue)

**Response bodies are camelCase** (`syncStatus`, `lastSyncedAt`, `externalId`),
but **request bodies currently require snake_case** and reject unknown keys
(`app/modules/sources/schemas.py` `_Request` sets `extra="forbid"` with no
camelCase alias generator). So a client that echoes a camelCase response field
back in a request body gets a `422`.

Until the request models adopt the project's `CamelRequestModel` convention,
**send request bodies in snake_case** exactly as shown below (`external_id`,
`lookback_days`). This is flagged for a backend fix; this doc documents current
behavior.

### Providers

Registered providers (`app/integrations/__init__.py`):

```txt
notion, github, slack, google_drive, gmail, zendesk, jira
```

---

## Auth API

Passwordless email auth plus OAuth SSO. These endpoints are **unauthenticated**
(no Bearer token) and rate limited to `10/minute` per IP (OAuth callback:
`20/minute`). The refresh token is returned in the JSON body **and** set as an
`httpOnly`, `secure`, `samesite=strict` cookie scoped to `/api/v1/auth`, so a
cookie-based client can call `refresh`/`logout` with an empty body.

Access tokens are HS256 JWTs, ~15 min TTL; refresh tokens last 30 days and
**rotate on every use** (reuse of a revoked token revokes the whole family).

### Sign up

```http
POST /api/v1/auth/signup
Content-Type: application/json
```

```json
{ "email": "dana@riverline.io" }
```

Response `201` — a fresh account with **no** workspace yet, so `nextStep` is
`onboarding`:

```json
{
  "data": {
    "user": { "id": "usr_…", "email": "dana@riverline.io", "name": null },
    "workspace": null,
    "accessToken": "eyJ…",
    "refreshToken": "eyJ…",
    "nextStep": "onboarding"
  },
  "meta": { "requestId": "…", "timestamp": "…" }
}
```

Errors: `409` if the email is already registered; `422` on an invalid email.

### Sign in

```http
POST /api/v1/auth/signin
Content-Type: application/json
```

```json
{ "email": "dana@riverline.io" }
```

Response `200` — same shape as signup. `workspace` is populated and `nextStep`
is `dashboard` when the user already has a workspace; otherwise `workspace` is
`null` and `nextStep` is `onboarding`.

Errors: `404` if no account exists for the email.

### Start OAuth SSO (`google` / `github`)

```http
GET /api/v1/auth/oauth/{provider}/start?mode=signup
```

`mode` ∈ `signup | signin` (default `signin`). Pass an optional
`Authorization: Bearer <accessToken>` to **link** a provider to the signed-in
user. Response `200`:

```json
{
  "data": {
    "authorizationUrl": "https://accounts.google.com/o/oauth2/v2/auth?…",
    "state": "eyJ…"
  },
  "meta": { "requestId": "…", "timestamp": "…" }
}
```

**Frontend flow:** redirect the browser to `authorizationUrl`. Errors: `400`
`invalid_provider` for anything but `google`/`github`; `501` for `saml` (accepted
but not yet implemented).

### OAuth callback

```http
POST /api/v1/auth/oauth/{provider}/callback
Content-Type: application/json
```

```json
{ "code": "oauth_code", "state": "eyJ…" }
```

Response `200`: the same `{ user, workspace, accessToken, refreshToken, nextStep }`
session shape as signin. The server runs seven `state` checks (signature, expiry,
single-use, provider match, redirect-URI match, and user-linking); any failure
returns a generic `401` that never reveals which check failed. A failed provider
exchange returns `502` `provider_error`.

### Refresh

```http
POST /api/v1/auth/refresh
Content-Type: application/json
```

Send the token in the body, **or** an empty body when relying on the cookie:

```json
{ "refreshToken": "eyJ…" }
```

Response `200`: `{ "data": { "accessToken": "eyJ…", "refreshToken": "eyJ…" } }`.
A new refresh cookie is set. Errors: `401` on a missing, expired, revoked, or
reused token — the response also **clears** the refresh cookie so a cookie client
stops looping on a dead token.

### Logout

```http
POST /api/v1/auth/logout
Content-Type: application/json
```

Body optional (`{ "refreshToken": "eyJ…" }`) or empty (cookie). Revokes the
presented refresh token and clears the cookie. **Idempotent** — an unknown or
already-revoked token still returns `204` (no token-probing oracle).

Response `204`: no body.

---

## Sources API

### List connected sources

```http
GET /api/v1/sources
Authorization: Bearer <accessToken>
```

Response `200`:

```json
{
  "data": [
    {
      "id": "src_01J…",
      "provider": "slack",
      "name": "Riverline",
      "status": "connected",
      "syncStatus": "healthy",
      "externalAccountId": "T012AB3CD",
      "lastSyncedAt": "2026-07-04T10:18:00.000Z",
      "health": 98,
      "createdAt": "2026-07-01T09:00:00.000Z"
    }
  ],
  "meta": { "requestId": "…", "timestamp": "…" }
}
```

Field notes: `externalAccountId`, `lastSyncedAt`, and `health` are nullable
(`null` before the first sync or when the provider returns no account id).

---

### Start OAuth (get consent URL)

```http
POST /api/v1/sources/{provider}/authorize
Authorization: Bearer <accessToken>
Content-Type: application/json
```

Request body — optional; only needed for **subdomain-scoped providers**
(currently Zendesk), omitted otherwise:

```json
{ "subdomain": "acme" }
```

For Notion / GitHub / Slack / Google / Jira, send **no body** (or `{}`).

Response `202`:

```json
{
  "data": { "authorizeUrl": "https://slack.com/oauth/v2/authorize?…" },
  "meta": { "requestId": "…", "timestamp": "…" }
}
```

**Frontend flow:** take `authorizeUrl` and redirect the browser to it
(`window.location.href = authorizeUrl`). Do not fetch it with XHR — it is a
full-page provider consent page.

Errors: `422` if `provider` is unknown, or if a Zendesk `subdomain` is
missing/invalid (must match `^[a-z0-9][a-z0-9-]{0,61}[a-z0-9]$`).

---

### OAuth callback (browser redirect target — not called by the frontend)

```http
GET /api/v1/sources/{provider}/callback?state=…&code=…
```

The provider redirects the user's browser here after consent. The server
verifies `state`, exchanges the `code` for tokens, stores the connection, and
responds `302` back to the dashboard:

```txt
Location: {FRONTEND_URL}/settings/sources?connected={provider}
```

Query params: `state` (required), `code` (optional — a GitHub App install
redirects with `installation_id` and no `code`), `installation_id` (optional,
GitHub), `error` (optional — set when the user declines the consent screen, e.g.
`error=access_denied`; the server redirects back to the dashboard cleanly
instead of attempting a token exchange). The frontend does not call this; it
only needs to render the `?connected={provider}` landing on `/settings/sources`
and refetch the source list. Rate limited to `20/minute`.

Errors surfaced during the redirect: `401` if the `state` is invalid, expired,
or already used.

---

### List a source's channels

```http
GET /api/v1/sources/{source_id}/channels
Authorization: Bearer <accessToken>
```

Merges the channels/pages/repos the provider currently exposes with the
selection state persisted for this connection. `id` is `null` for a
discovered-but-never-persisted channel.

Response `200`:

```json
{
  "data": [
    {
      "id": "chn_01J…",
      "externalId": "C012AB3CD",
      "name": "cs-escalations",
      "selected": true,
      "itemCount": 880
    },
    {
      "id": null,
      "externalId": "C09ZZ",
      "name": "random",
      "selected": false,
      "itemCount": 0
    }
  ],
  "meta": { "requestId": "…", "timestamp": "…" }
}
```

Errors: `404` if `source_id` is not a connection in this workspace.

---

### Select channels + set lookback

```http
PATCH /api/v1/sources/{source_id}/channels
Authorization: Bearer <accessToken>
Content-Type: application/json
```

Request body — **snake_case** (see casing note above). `lookback_days` is
optional (`1`–`730`); omit to keep the current value:

```json
{
  "channels": [
    { "external_id": "C012AB3CD", "name": "cs-escalations", "selected": true },
    { "external_id": "C09ZZ", "name": "random", "selected": false }
  ],
  "lookback_days": 90
}
```

Response `200` — the full persisted channel list after the update (same shape as
the GET above):

```json
{
  "data": [
    {
      "id": "chn_01J…",
      "externalId": "C012AB3CD",
      "name": "cs-escalations",
      "selected": true,
      "itemCount": 0
    }
  ],
  "meta": { "requestId": "…", "timestamp": "…" }
}
```

Errors: `404` unknown source; `422` on unknown/camelCase keys or
`lookback_days` out of `[1, 730]`.

---

### Disconnect a source

```http
POST /api/v1/sources/{source_id}/disconnect
Authorization: Bearer <accessToken>
```

Best-effort provider-side token revoke, stops any push-channel renewals, then
deletes the connection.

Response `204`: no body.

Errors: `404` if `source_id` is not a connection in this workspace.

---

## Sweeps API

The onboarding backfill across all connected sources. Drives the "Building your
brain…" screen.

### Start the onboarding sweep

```http
POST /api/v1/sweeps
Authorization: Bearer <accessToken>
```

No request body. Idempotent per workspace: if a sweep is already pending/running,
that one is returned instead of starting a second.

Response `202` when a new sweep is created, `200` when an in-flight one is
returned:

```json
{
  "data": {
    "id": "swp_01J…",
    "status": "pending",
    "progress": {},
    "skillsCreated": 0,
    "skillsQueued": 0,
    "startedAt": "2026-07-05T10:20:00.000Z",
    "completedAt": null
  },
  "meta": { "requestId": "…", "timestamp": "…" }
}
```

**Frontend flow:** POST once, then poll `GET /sweeps/{id}` until `status` is
terminal.

---

### Get sweep status

```http
GET /api/v1/sweeps/{sweep_id}
Authorization: Bearer <accessToken>
```

Response `200`:

```json
{
  "data": {
    "id": "swp_01J…",
    "status": "running",
    "progress": {
      "slack":  { "status": "completed", "inserted": 1280 },
      "notion": { "status": "running",   "inserted": 40 },
      "github": { "status": "failed",    "inserted": 0, "error": "rate_limited" }
    },
    "skillsCreated": 12,
    "skillsQueued": 3,
    "startedAt": "2026-07-05T10:20:00.000Z",
    "completedAt": null
  },
  "meta": { "requestId": "…", "timestamp": "…" }
}
```

`progress` is a per-provider map:
`{ provider: { status: "running" | "completed" | "failed", inserted: int, error?: str } }`.
`completedAt` is set once the sweep finishes.

Errors: `404` if `sweep_id` is unknown in this workspace. Note: a non-UUID
`sweep_id` currently returns `500` rather than `404` (flagged for a backend fix).

---

## Workspaces API

Workspace creation and configuration. All routes are workspace-scoped except
`POST /workspaces` (which runs on the signup token before a workspace exists).

### Create workspace

```http
POST /api/v1/workspaces
Authorization: Bearer <accessToken>
Content-Type: application/json
```

Only a valid JWT is required (the signup token carries `workspaceId=null`).
Request body — **snake_case**:

```json
{ "company_name": "Riverline", "team_size": "51-200", "primary_use_case": "support" }
```

`team_size` ∈ `1-10 | 11-50 | 51-200 | 200+`;
`primary_use_case` ∈ `support | ops | eng | agents`.

Response `201` — includes a **fresh access token** already scoped to the new
workspace, so the client can make workspace-scoped calls immediately without
re-signing in:

```json
{
  "data": {
    "workspace": { "id": "wrk_…", "name": "Riverline", "slug": "riverline", "plan": "trial" },
    "accessToken": "eyJ…"
  },
  "meta": { "requestId": "…", "timestamp": "…" }
}
```

Errors: `422` on a missing/invalid field or unknown/camelCase key.

### Save onboarding progress

```http
PATCH /api/v1/workspaces/{workspace_id}/onboarding
Authorization: Bearer <accessToken>   # admin
Content-Type: application/json
```

Records workspace-level onboarding progress. Request body — **snake_case**;
`step` is required, the rest optional:

```json
{
  "step": "configure",
  "company_name": "Riverline",
  "team_size": "51-200",
  "primary_use_case": "support",
  "connected_providers": ["slack", "notion"],
  "time_range": "90d"
}
```

`step` ∈ `company | connect | configure | build | done`;
`time_range` ∈ `30d | 90d | 6mo | all`. Response `200`:
`{ "data": { "status": "saved", "nextStep": "…" } }`.

> **Note:** `connected_providers` / `time_range` / `channels` are **accepted but
> not persisted here** — provider/channel/lookback selection is owned by the
> Sources API (`PATCH /sources/{source_id}/channels`). This endpoint only records
> the wizard `step` and company fields. Prefer the Sources route for scope.

Errors: `403` if the JWT is not an admin of `{workspace_id}`; `422` on a bad
`step`/`time_range`.

### Get settings

```http
GET /api/v1/workspaces/{workspace_id}/settings
Authorization: Bearer <accessToken>   # any member
```

Response `200`:

```json
{
  "data": {
    "workspace": { "name": "Riverline", "domain": "riverline.io", "plan": "trial", "seatLimit": 10 },
    "brainEndpoint": "https://riverline.brainites.com/mcp"
  },
  "meta": { "requestId": "…", "timestamp": "…" }
}
```

`workspace.domain` is nullable. `brainEndpoint` is the per-workspace MCP URL.

### Update settings

```http
PATCH /api/v1/workspaces/{workspace_id}/settings
Authorization: Bearer <accessToken>   # admin
Content-Type: application/json
```

Partial update; send only the fields you change. A blank `domain` clears it;
`name` may not be null.

```json
{ "name": "Riverline", "domain": "riverline.io" }
```

Response `200`: `{ "data": { "workspace": { "id": "wrk_…", "name": "Riverline", "domain": "riverline.io" } } }`.
Errors: `403` if not an admin of `{workspace_id}`; `422` on a null `name` or
unknown key.

---

## Dashboard API

Two read-only endpoints for the home screen. Both require a workspace **member**
(non-members get `403`) and are rate limited to `300/minute` per user.

### Overview

```http
GET /api/v1/workspaces/{workspace_id}/overview
Authorization: Bearer <accessToken>
```

Response `200` — the aggregated home payload. `data` contains:

| Field | Shape |
| --- | --- |
| `workspace` | `{ name, slug, plan }` |
| `greetingName` | string |
| `sync` | `{ status: healthy\|syncing\|pending\|error, label, lastSyncedAt }` |
| `kpis[]` | `{ id: decisions\|policies\|skills\|reviews, label, value, trend, spark[] }` |
| `recentQuestions[]` | string[] |
| `reviewPreview[]` | review summaries (see Members/Reviews shape below) |
| `recentDecisions[]` | decision summaries |
| `sourceHealth[]` | `{ id, provider, name, status, syncStatus, lastSyncedAt, health, pendingItems, activeChannelCount, extractedLabel }` |
| `activity[]` | `{ id, type, title, detail, sourceProvider, createdAt }` |

`reviewPreview[]` items:
`{ id, title, kind, sourceProvider, sourceLocation, before, after, evidenceQuote, evidenceAuthor, confidence, status }`.
`recentDecisions[]` items:
`{ id, title, sourceProvider, sourceLocation, status, confidence, category, owner { name, avatarColor }, monthlyUses, updatedAt, summary, rule }`.

> These are read-only **previews** embedded in the overview. Full Decisions and
> Reviews list/detail/mutation APIs are **not shipped yet** (`MISSING_APIS.md`).

### Activity feed

```http
GET /api/v1/workspaces/{workspace_id}/activity?limit=20&cursor=…
Authorization: Bearer <accessToken>
```

`limit` default `20`, max `100`; `cursor` optional. Response `200`:

```json
{
  "data": [
    { "id": "act_…", "type": "decision_extracted", "title": "…", "detail": "…", "sourceProvider": "slack", "createdAt": "…" }
  ],
  "meta": { "requestId": "…", "timestamp": "…", "nextCursor": "…" }
}
```

`meta.nextCursor` is `null` on the last page.

---

## Members API

### List members

```http
GET /api/v1/workspaces/{workspace_id}/members
Authorization: Bearer <accessToken>   # any member
```

Response `200` — roster plus seat meta:

```json
{
  "data": [
    {
      "id": "usr_…",
      "name": "Dana Rivers",
      "email": "dana@riverline.io",
      "role": "admin",
      "title": "Head of Support",
      "avatarColor": "#7C3AED",
      "isCurrentUser": true
    }
  ],
  "meta": { "requestId": "…", "timestamp": "…", "seatLimit": 10, "usedSeats": 4, "pendingInvites": 1 }
}
```

`role` ∈ `admin | editor | viewer`. `name`, `title`, `avatarColor` are nullable.

### Invite member

```http
POST /api/v1/workspaces/{workspace_id}/members/invite
Authorization: Bearer <accessToken>   # admin
Content-Type: application/json
```

```json
{ "email": "sam@riverline.io", "role": "viewer" }
```

`role` ∈ `admin | editor | viewer` (default `viewer`). Response `201`:

```json
{
  "data": { "inviteId": "inv_…", "email": "sam@riverline.io", "role": "viewer", "status": "pending" },
  "meta": { "requestId": "…", "timestamp": "…" }
}
```

The raw invite token is never returned. Errors: `403` if not an admin; `422` on
an invalid email/role.

---

## API Keys API

Agent / MCP credentials. All routes require the **admin** role. The raw key is
`hph_live_<32 hex>` and is shown **exactly once** on create — only a SHA-256 hash
and a short `prefix` are stored. Creation is rate limited to `5/hour` per admin.

### List keys

```http
GET /api/v1/workspaces/{workspace_id}/api-keys
Authorization: Bearer <accessToken>   # admin
```

Response `200` — never carries the raw or hashed key:

```json
{
  "data": [
    {
      "id": "key_…",
      "name": "Production MCP client",
      "prefix": "hph_live_a1b2",
      "scopes": ["brain:query", "skills:invoke"],
      "createdAt": "…",
      "lastUsedAt": "…"
    }
  ],
  "meta": { "requestId": "…", "timestamp": "…" }
}
```

`lastUsedAt` is `null` until the key is first used.

### Create key

```http
POST /api/v1/workspaces/{workspace_id}/api-keys
Authorization: Bearer <accessToken>   # admin
Content-Type: application/json
```

```json
{ "name": "Production MCP client", "scopes": ["brain:query", "skills:invoke"] }
```

`scopes` ∈ `brain:query | skills:invoke | sources:read | decisions:read`
(at least one; duplicates are de-duped). Response `201` — `apiKey` is the **only**
time the raw secret is returned:

```json
{
  "data": {
    "id": "key_…",
    "name": "Production MCP client",
    "apiKey": "hph_live_a1b2c3d4e5f6…",
    "prefix": "hph_live_a1b2",
    "scopes": ["brain:query", "skills:invoke"],
    "createdAt": "…"
  },
  "meta": { "requestId": "…", "timestamp": "…" }
}
```

Handle `apiKey` in dialog state; never cache it. Errors: `422` on an empty
`scopes` or an unknown scope; `429` after 5 creates in an hour.

### Revoke key

```http
DELETE /api/v1/workspaces/{workspace_id}/api-keys/{key_id}
Authorization: Bearer <accessToken>   # admin
```

Revokes immediately. Response `204`: no body.

---

## Frontend wiring summary

| UI step                     | Call(s)                                                              |
| --------------------------- | ------------------------------------------------------------------- |
| Email sign-up / sign-in     | `POST /auth/signup` \| `POST /auth/signin`                          |
| OAuth SSO                   | `GET /auth/oauth/{provider}/start` → redirect → `POST /auth/oauth/{provider}/callback` |
| Silent token refresh        | `POST /auth/refresh` (single-flight)                               |
| Sign out                    | `POST /auth/logout`                                                |
| Create workspace (onboarding) | `POST /workspaces` → use returned `accessToken`                  |
| Save onboarding step        | `PATCH /workspaces/{workspaceId}/onboarding`                       |
| Home / dashboard            | `GET /workspaces/{workspaceId}/overview`                          |
| Activity feed (paginated)   | `GET /workspaces/{workspaceId}/activity?cursor=…`                 |
| Settings page               | `GET` / `PATCH /workspaces/{workspaceId}/settings`               |
| Members page                | `GET /workspaces/{workspaceId}/members` \| `POST …/members/invite` |
| API keys page               | `GET` / `POST /workspaces/{workspaceId}/api-keys` \| `DELETE …/{keyId}` |
| Sources list / health       | `GET /sources`                                                      |
| Connect a provider          | `POST /sources/{provider}/authorize` → redirect to `authorizeUrl`   |
| Return from provider        | browser lands on `/settings/sources?connected={provider}` → refetch `GET /sources` |
| Channel picker              | `GET /sources/{sourceId}/channels`                                  |
| Save channel + lookback     | `PATCH /sources/{sourceId}/channels`                                |
| Remove a provider           | `POST /sources/{sourceId}/disconnect`                               |
| Start onboarding backfill   | `POST /sweeps`                                                      |
| "Building your brain…" poll | `GET /sweeps/{sweepId}`                                             |

---

## Webhooks (server-side only — not a frontend call)

```http
POST /api/v1/webhooks/{provider}
```

Provider→server delivery, authenticated by per-provider signature verification
(no dashboard JWT). Responds `200 {"data": {"status": "accepted"}}` immediately;
Slack's `url_verification` handshake echoes `{"challenge": "…"}`. The frontend
never calls these — listed only so the full integration surface is in one place.
