# Source Connect — Return-URL Requirement for the Backend

Raised while wiring the **onboarding connect step** (`INTEGRATIONS.md` §Sources)
into the frontend. One request: let the caller say **where the browser should
land after the OAuth exchange**, instead of the callback always redirecting to
`/settings/sources`.

Ordered: problem → proposed contract → constraints → acceptance criteria.

---

## The problem

`GET /api/v1/sources/{provider}/callback` finishes the exchange and always
responds:

```txt
Location: {FRONTEND_URL}/settings/sources?connected={provider}
```

That destination is hardcoded. It is correct for a user who started the connect
from **Settings → Sources**, and wrong for a user who started it from the
**onboarding wizard**, which is the majority of first connects.

What the user sees today: on the onboarding "Connect your sources" step they
click _Connect Slack_, consent on Slack's screen, and land on the **dashboard**
Sources page — dropped out of onboarding mid-flow, with the remaining steps
(configure → build → ready → integrate) skipped.

### Why the frontend can't fix this alone

1. `POST /sources/{provider}/authorize` accepts no return-to input — the only
   body field is the optional Zendesk `subdomain`
   (`INTEGRATIONS.md:288-295`), so the FE has no way to tell the server where
   it came from.
2. Handing the browser to `authorizeUrl` is a **full-page** navigation
   (`src/features/sources/hooks/useConnectSource.ts:34`), per the contract's own
   "do not fetch it with XHR" note. The SPA is torn down; all in-memory state,
   including the onboarding step index, is lost.
3. The app has to keep a throwaway route (`/settings/sources` →
   `SourcesCallbackRedirect`, `src/app/router/index.tsx:24-27`) purely to
   forward the hardcoded path onto the real page at `/dashboard/sources`. The
   BE's redirect target is not a path this app actually serves.

The FE can work around it with `sessionStorage` (stash a return marker before
the redirect, read it on the way back), but that is state living outside the
OAuth flow — it breaks if consent completes in a different tab, and it leaves
the BE redirecting to a path the app doesn't serve. We'd rather the round trip
carry its own destination.

---

## Proposed contract change

### 1. Accept `returnTo` on authorize

```http
POST /api/v1/sources/{provider}/authorize
Authorization: Bearer <accessToken>
Content-Type: application/json
```

```json
{ "returnTo": "/onboarding" }
```

- **Optional.** Omitted → current behaviour (`/settings/sources`) is unchanged,
  so nothing that exists today breaks.
- Combinable with `subdomain` for Zendesk:
  `{ "subdomain": "acme", "returnTo": "/onboarding" }`.
- Please confirm the casing you want — `returnTo` or `return_to`. The docs show
  camelCase request bodies but also state a global snake_case + `extra="forbid"`
  rule; same ambiguity we hit on `/auth/refresh` (see `BE_AUTH_QUESTIONS.md`).

### 2. Carry it through `state`

`returnTo` must survive the trip to the provider and back. It should be bound to
the existing signed, single-use `state` (server-side lookup keyed by `state`, or
sealed inside it) — **not** appended to the provider's `redirect_uri`, which
must stay byte-identical to what's registered with each provider.

Binding it to `state` also means it inherits the existing tamper protection: a
forged `returnTo` fails the same `401` that a forged `state` does.

### 3. Redirect there on the way back

Success:

```txt
Location: {FRONTEND_URL}{returnTo}?connected={provider}
```

Declined / failed consent — same destination, existing error shape:

```txt
Location: {FRONTEND_URL}{returnTo}?error=access_denied
```

The error leg matters as much as the success leg: a user who declines Slack
during onboarding should return to the onboarding step, not be ejected to the
dashboard. The `?connected=` / `?error=` params are unchanged — the FE already
handles both on any surface
(`src/features/sources/hooks/useConnectionLanding.ts`).

---

## Constraints

**Open redirect is the risk here.** `returnTo` is attacker-influencable input
that ends up in a `Location` header, so please:

- Accept **relative paths only** — must start with a single `/`, and reject
  anything starting with `//`, containing a scheme (`http:`, `https:`,
  `javascript:`), a host, or a backslash.
- Prefer a **server-side allowlist** over free-form validation. Two values cover
  every case we have today:

  | `returnTo`           | Used by                           |
  | -------------------- | --------------------------------- |
  | `/onboarding`        | onboarding connect step           |
  | `/dashboard/sources` | Settings → Sources (see §3 below) |

  Anything not on the list → fall back to the default rather than `422`, so a
  stale FE build degrades to today's behaviour instead of failing the connect.

- Resolve against `FRONTEND_URL` server-side. Never echo a caller-supplied
  origin.

---

## Bonus: fix the default while you're in there

The current default `/settings/sources` is a path this frontend does not serve —
we redirect it to `/dashboard/sources`. If the default becomes
**`/dashboard/sources`**, we can delete the `SourcesCallbackRedirect` route
entirely.

This one is a **breaking change** for any client relying on the old path, so
treat it as separate from the `returnTo` work — ship `returnTo` first if that's
easier, and we'll keep the forwarding route until the default moves.

---

## Acceptance criteria

- [ ] `POST /sources/{provider}/authorize` accepts an optional `returnTo`;
      omitting it preserves today's redirect exactly.
- [ ] `returnTo` is bound to the signed `state`, not to the provider
      `redirect_uri`.
- [ ] Success redirects to `{FRONTEND_URL}{returnTo}?connected={provider}`.
- [ ] Decline/error redirects to `{FRONTEND_URL}{returnTo}?error=…`.
- [ ] Non-allowlisted / absolute / protocol-relative values fall back to the
      default; no external host is ever reachable via `returnTo`.
- [ ] Works for every provider, including the GitHub App install leg that
      returns `installation_id` with no `code`.
- [ ] `INTEGRATIONS.md` §Sources updated (§"Start OAuth" body and §"OAuth
      callback" `Location`), plus the flow table at the bottom (line ~837).

---

## What the frontend does on delivery

- Onboarding's connect step sends `returnTo: "/onboarding"`; Settings → Sources
  sends `/dashboard/sources`.
- Onboarding resumes on the connect step from the `?connected=` landing, the
  same way an in-flight sweep already resumes via `GET /sweeps/active`.
- We drop the `sessionStorage` workaround, and — once the default moves — the
  `/settings/sources` forwarding route.

Until then the FE ships the `sessionStorage` workaround so onboarding isn't
broken; it's self-contained and easy to remove.
