# Auth Contract (BE ↔ FE)

Authoritative answers to the frontend's auth questions, from the backend code.
The live wire contract is always **`docs/openapi.json`** (run `make docs` to
refresh) — this file explains the parts a schema can't.

## TL;DR for the frontend

- Send/receive **camelCase** everywhere. `accessToken` / `refreshToken`.
- Store the **access token** in memory/localStorage; **do not** store the refresh
  token — it lives in an httpOnly cookie the JS can't read.
- Call refresh with **`credentials: "include"`** and an empty body; the cookie
  carries the token.
- On reload, call **`GET /api/v1/auth/me`** to rehydrate — don't trust a
  localStorage snapshot.

## 1. Request casing on `/auth/refresh` and `/auth/logout`

Non-issue. Both bodies extend `CamelRequestModel` (`populate_by_name=True`,
`alias_generator=to_camel`, `extra="forbid"`), so the field is accepted as **either
`refreshToken` or `refresh_token`**; unknown keys → `422`. Responses are always
camelCase (`accessToken`, `refreshToken`). The body is **optional** — with the
cookie flow, send an empty body.

- `POST /auth/refresh` → `200 { data: { accessToken, refreshToken } }`, and sets a
  fresh refresh cookie.
- `POST /auth/logout` → `204`, clears the cookie. Idempotent.

## 2. CORS & the refresh cookie

- CORS: `allow_credentials=True`; `allow_origins` = `ALLOWED_ORIGINS` env.
  **The FE origin must be listed** (no wildcard with credentials). See
  `.env.example`.
- Cookie (`refresh_token`): `httpOnly`, `Secure`, `SameSite` **env-driven**, `Path=/api/v1/auth`.
  - **dev** → `SameSite=Strict` (FE + BE share the `localhost` site; `Secure` is allowed on localhost).
  - **production** (`ENVIRONMENT=production`) → `SameSite=None` automatically, for a
    cross-site FE/BE. If your FE and BE are **same-site subdomains** (e.g.
    `app.` + `api.example.com`), `Strict` would also work — the default errs safe
    for the cross-domain case.
- FE must fetch `/auth/refresh` and `/auth/logout` with `credentials: "include"`.

## 3. `GET /auth/me` (added)

Rehydrate session state from the access token. Requires a valid **dashboard JWT**
(not an API key) in `Authorization: Bearer <accessToken>`.

```http
GET /api/v1/auth/me
200 { data: {
  user: { id, email, name },
  workspace: { id, name, slug } | null,
  role: "viewer" | "editor" | "admin" | null,
  nextStep: "onboarding" | "dashboard"
} }
401 when the token is missing/invalid/expired.
```

`workspace`/`role` are `null` before onboarding (`nextStep: "onboarding"`).

## 4. OAuth redirect URIs — FE vs BE

Two distinct flows, do not conflate:

- **Login SSO** (Google/GitHub) is **frontend-driven**. The backend builds the
  provider `redirect_uri` as **`{FRONTEND_URL}{FRONTEND_OAUTH_CALLBACK_PATH}`**
  (default `http://localhost:3000/auth/callback`) — a **frontend** page, not a
  backend route. Register exactly that URL as the Authorized redirect URI in the
  Google/GitHub console.

  Flow:
  1. FE calls `GET /auth/oauth/{provider}/start?mode=signin|signup` → `{ authorizationUrl, state }`.
  2. FE remembers the provider (e.g. sessionStorage) and redirects the browser to `authorizationUrl`.
  3. Provider bounces back to `{FRONTEND_URL}/auth/callback?code=…&state=…`.
  4. The FE callback page POSTs `{ code, state }` to `POST /auth/oauth/{provider}/callback`.
  5. BE verifies state, exchanges the code, upserts the user, returns a session (+ sets the refresh cookie) — same shape as signin.

  **Setup (Google):** Cloud Console → OAuth consent screen (scopes `openid email
  profile`; add test users while in Testing) → Credentials → OAuth client ID (Web
  application) → Authorized redirect URI = the FE callback URL above → put the
  client id/secret in `LOGIN_GOOGLE_CLIENT_ID` / `LOGIN_GOOGLE_CLIENT_SECRET`.

- **Source connectors** (Slack/Notion/GitHub-App/Google/Zendesk/Jira) use **backend**
  GET callbacks built from `OAUTH_REDIRECT_BASE_URL`
  (`{base}/api/v1/sources/{provider}/callback`). These are not the login flow.

Align the console redirect URIs with `FRONTEND_URL` + `FRONTEND_OAUTH_CALLBACK_PATH`
(login) and `OAUTH_REDIRECT_BASE_URL` (connectors) for each environment.

## Endpoint summary

| Method | Path | Auth | Notes |
|---|---|---|---|
| POST | `/auth/signup` | none | `{ email }` → session + `nextStep` |
| POST | `/auth/signin` | none | `{ email }` → session + `nextStep` |
| GET | `/auth/me` | Bearer JWT | rehydrate on reload |
| POST | `/auth/refresh` | cookie or `{ refreshToken }` | rotates the pair |
| POST | `/auth/logout` | cookie or `{ refreshToken }` | 204, idempotent |
| GET | `/auth/oauth/{provider}/start` | optional Bearer | `{ authorizationUrl, state }` |
| POST | `/auth/oauth/{provider}/callback` | optional Bearer | `{ code, state }` → session |

Providers: `google`, `github` (login); `saml` accepted but not yet implemented.
