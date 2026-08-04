# Integrations API — Frontend Contract

The endpoints the dashboard frontend needs to connect a source, pick what to
ingest, and drive the onboarding sweep. Everything here is **implemented** in
`brain-api/app/modules/{sources,sweeps}` and mounted under `/api/v1` by
`app/main.py` — this is the real wire contract, not the aspirational shapes in
`API_DOCUMENTATION.md`.

Two modules are frontend-facing:

- **Sources** (`/api/v1/sources`) — connect / list / disconnect providers and
  choose channels + lookback window.
- **Sweeps** (`/api/v1/sweeps`) — start and poll the onboarding backfill.

**Webhooks** (`/api/v1/webhooks/{provider}`) also exist but are **not**
frontend-facing — they are provider→server callbacks authenticated by signature,
not the dashboard JWT. They are listed at the end for completeness only.

---

## Conventions

### Base URL

```txt
Local:      http://localhost:4000/api/v1
Production: https://api.brainite.com/api/v1
```

### Auth

Every route below (except the OAuth callback) requires an **admin** dashboard JWT
— `source_connections` and `sweeps` are admin-only at the RLS layer:

```http
Authorization: Bearer <accessToken>
```

A non-admin token is rejected with `403` before the handler runs. The OAuth
**callback** carries no JWT (it is a browser redirect from the provider); it
authenticates via the signed, single-use `state` instead.

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

Request body — optional. `subdomain` is only needed for **subdomain-scoped
providers** (currently Zendesk); `returnTo` picks the frontend path the OAuth
callback redirects back to:

```json
{ "subdomain": "acme", "returnTo": "/onboarding" }
```

For Notion / GitHub / Slack / Google / Jira with a default landing, send
**no body** (or `{}`).

`returnTo` (camelCase; `return_to` also accepted) is checked against a
server-side allowlist — currently `/onboarding` and `/dashboard/sources`. It is
bound to the single-use OAuth `state` server-side (never appended to the
provider `redirect_uri`). Omitted or non-allowlisted values silently fall back
to the default `/settings/sources`, so a stale client degrades to today's
behaviour instead of failing the connect; no external host is ever reachable
through it.

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
responds `302` back to the frontend — at the `returnTo` path bound to the
state at authorize time, or `/settings/sources` when none was given:

```txt
Location: {FRONTEND_URL}{returnTo|/settings/sources}?connected={provider}
```

A declined/failed consent redirects to the same destination with the existing
error param:

```txt
Location: {FRONTEND_URL}{returnTo|/settings/sources}?error={provider}
```

(The single-use `state` is left unconsumed on a decline, so a browser retry
doesn't hit a spurious "state already used".)

Query params: `state` (required), `code` (optional — a GitHub App install
redirects with `installation_id` and no `code`), `installation_id` (optional,
GitHub), `error` (optional — set when the user declines the consent screen, e.g.
`error=access_denied`; the server redirects back cleanly instead of attempting
a token exchange). The frontend does not call this; it only needs to render the
`?connected={provider}` / `?error={provider}` landing on whichever path it sent
as `returnTo` and refetch the source list. Rate limited to `20/minute`.

Errors surfaced during the redirect: `401` if the `state` is invalid, expired,
or already used.

---

### Read a source's scope (channels + lookback)

```http
GET /api/v1/sources/{source_id}/channels
Authorization: Bearer <accessToken>
```

Returns the connection's whole scope: the channels/pages/repos the provider
currently exposes merged with the selection state persisted for this connection,
plus the persisted `lookbackDays`. `id` is `null` for a
discovered-but-never-persisted channel. `lookbackDays` is always a concrete
number (the column is NOT NULL, default `90`), so the client can pre-select the
saved window instead of showing "unchanged".

Response `200`:

```json
{
  "data": {
    "channels": [
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
    "lookbackDays": 90
  },
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

Response `200` — the full persisted scope after the update (same shape as the GET
above). `lookbackDays` echoes what is now stored: the value just written, or the
untouched current value when the key was omitted. Seed the client's cache from
this instead of refetching.

```json
{
  "data": {
    "channels": [
      {
        "id": "chn_01J…",
        "externalId": "C012AB3CD",
        "name": "cs-escalations",
        "selected": true,
        "itemCount": 0
      }
    ],
    "lookbackDays": 90
  },
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

## Frontend wiring summary

| UI step                     | Call(s)                                                              |
| --------------------------- | ------------------------------------------------------------------- |
| Sources list / health       | `GET /sources`                                                      |
| Connect a provider          | `POST /sources/{provider}/authorize` (body may carry `returnTo`) → redirect to `authorizeUrl` |
| Return from provider        | browser lands on `{returnTo|/settings/sources}?connected={provider}` (or `?error={provider}`) → refetch `GET /sources` |
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
