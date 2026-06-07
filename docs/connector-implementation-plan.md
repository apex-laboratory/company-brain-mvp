# Connector Implementation Plan — KAN-2

Source Integrations: Connectors + Context Expanders

All 5 connectors (Slack, Jira, GitHub, Notion, Zendesk) are built entirely in-house.
Customers see only our UI and the provider's own OAuth consent screen — no third-party tooling
is exposed. dlt is used as an invisible Python extraction library inside the sweep worker.

---

## Architecture

```
Your settings UI
    │  "Connect Slack" button
    ▼
brain-api /oauth/{source}/authorize   ← generates state + PKCE, redirects to provider
    │
    ▼  (provider's own OAuth consent screen)
    │
brain-api /oauth/{source}/callback    ← exchanges code for tokens, stores encrypted
    │
    ▼
source_connections table (access_token_enc, refresh_token_enc, expires_at)
    │
    ├── dlt pipeline (scheduled sweep — reads token from DB, fetches incrementally)
    │
    └── /webhooks/{source} receiver (HMAC verified, enqueues to worker)
```

---

## File Structure

```
brain-api/
  connectors/
    base.py              ← BaseConnector abstract class
    oauth.py             ← per-provider authorize_url(), exchange_code(), refresh_token()
    slack.py
    github.py
    jira.py
    notion.py            ← polling only (Notion has no webhook API)
    zendesk.py
  routers/
    oauth.py             ← GET /oauth/{source}/authorize
                           GET /oauth/{source}/callback
    webhooks.py          ← POST /webhooks/{source}
```

---

## BaseConnector Interface

```python
class BaseConnector:
    def __init__(self, tenant_id: str, config: dict, state: dict): ...

    async def fetch(self) -> AsyncIterator[RawEvent]:
        """Yield normalized events since the state cursor."""
        ...

    def get_state(self) -> dict:
        """Return updated cursor for checkpointing (stored per sweep)."""
        ...

    async def refresh_token_if_needed(self) -> None:
        """Proactive refresh: if expires_at < now + 5min, refresh before fetching."""
        ...

    async def handle_auth_error(self, exc: Exception) -> None:
        """On 401: attempt one refresh + retry. On failure: mark AUTH_BROKEN."""
        ...
```

### Canonical Event Envelope

```python
@dataclass
class RawEvent:
    id: str                    # internal UUID
    tenant_id: str
    connector_type: str        # slack | jira | github | notion | zendesk
    source_id: str             # provider's original ID
    event_type: str            # message | issue | ticket | page | pr | comment
    actor: dict                # {id, email, name} — normalized
    content: str               # plain text, normalized from mrkdwn/ADF/blocks/MD
    created_at: datetime       # always UTC
    url: str                   # deep link back to source
    raw: dict                  # full original payload — preserved for re-processing
```

Preserve `raw` so the Groq extraction pipeline can reprocess without re-ingesting.

---

## OAuth Flow — Per Connector

### Endpoints

```
GET  /oauth/{source}/authorize          → redirect to provider auth URL
GET  /oauth/{source}/callback           → exchange code, store tokens, redirect to UI
POST /oauth/{source}/disconnect         → revoke token, delete source_connection row
```

### State + PKCE

- Generate a cryptographically random `state` param per authorization request
- Store `state` → `{tenant_id, source, code_verifier}` in Redis with 10-minute TTL
- Verify `state` on callback before exchanging the code
- Use PKCE (`code_verifier` / `code_challenge`) for all providers that support it

### Per-Provider OAuth Details

| Provider | Auth URL | Token URL | Special handling |
|---|---|---|---|
| **Slack** | `slack.com/oauth/v2/authorize` | `slack.com/api/oauth.v2.access` | Bot token auth; tokens don't expire by default |
| **GitHub** | `github.com/login/oauth/authorize` | `github.com/login/oauth/access_token` | GitHub Apps: JWT-signed installation token (~1hr TTL), re-issue via installations endpoint |
| **Jira** | `auth.atlassian.com/authorize` | `auth.atlassian.com/oauth/token` | After exchange, call `/oauth/token/accessible-resources` to get `cloudId` per workspace |
| **Notion** | `api.notion.com/v1/oauth/authorize` | `api.notion.com/v1/oauth/token` | Both `access_token` and `refresh_token` rotate on refresh — persist new pair atomically |
| **Zendesk** | `{subdomain}.zendesk.com/oauth/authorizations/new` | `{subdomain}.zendesk.com/oauth/tokens` | Subdomain is customer-specific — capture at connect time |

### Token Storage

Tokens are stored encrypted in `source_connections` using pgcrypto (`access_token_enc BYTEA`, `refresh_token_enc BYTEA`). The encryption key is `ENCRYPTION_KEY` from env.

```python
# Proactive refresh strategy
if connection.expires_at and connection.expires_at < datetime.utcnow() + timedelta(minutes=5):
    await connector.refresh_token_if_needed()

# Reactive fallback
try:
    data = await api_call(token)
except HTTP401:
    await connector.handle_auth_error()   # one refresh attempt
    data = await api_call(new_token)      # retry once
    # if refresh fails → mark status='auth_broken', surface to user
```

---

## Webhook Receiver

### Endpoint

```
POST /webhooks/{source}
```

### HMAC Verification (per provider)

| Provider | Header | Algorithm |
|---|---|---|
| Slack | `X-Slack-Signature` | HMAC-SHA256 of `v0:{timestamp}:{raw_body}` |
| GitHub | `X-Hub-Signature-256` | HMAC-SHA256 of raw body |
| Jira | `X-Hub-Signature` | HMAC-SHA256 of raw body |
| Zendesk | No HMAC | Verify via OAuth token in header |
| Notion | No webhooks | — |

**Critical:** verify against raw bytes before any JSON parsing. Reject if timestamp is >5 minutes old (replay protection).

### Idempotency

Deduplicate on `(org_id, source, external_event_id)` in `source_events` — the unique constraint is already in the schema. Return `200` on conflict without reprocessing.

### Processing

```
POST /webhooks/{source}
  │  1. Verify HMAC (raw bytes)
  │  2. Return 200 immediately
  │  3. Enqueue to Redis (Celery task)
  ▼
sweep worker
  │  4. Deduplicate via source_events unique constraint
  │  5. Normalize to RawEvent
  │  6. Groq 6-step extraction pipeline
```

---

## Data Extraction — dlt (sweep worker)

dlt is used as a Python library inside the sweep worker — customers never interact with it.
All 5 sources have verified dlt sources.

```python
import dlt

pipeline = dlt.pipeline(
    pipeline_name=f"brainite_{tenant_id}_{source}",
    destination="postgres",
    dataset_name=f"raw_{source}",
)

# Pass the decrypted token from source_connections at runtime
source = slack_source(access_token=decrypted_token, ...)
pipeline.run(source)
```

### Incremental Sync (cursor pattern)

Store cursor in `sweeps` table per (tenant, source):

```python
state = {"updated_at": "2024-01-15T10:00:00Z"}

# On each sweep: fetch records where updated_at > state["updated_at"] - lookback_window
# After sweep: advance cursor to max(updated_at) seen in this batch
# Lookback window: subtract 5 minutes to catch records with stale timestamps
```

---

## Per-Connector Notes

### Slack
- Use Bot Token (not User Token) — higher rate limits
- Rate limits are per-method per-workspace: Tier 1 (1/min) to Tier 4 (100/min)
- Streams: channels, messages, threads, reactions, users
- Webhooks via Events API; subscribe on connection

### GitHub
- Use GitHub App installation tokens (15,000 req/hr vs 5,000 for user tokens)
- Proactively throttle when `X-RateLimit-Remaining < 10%`
- Streams: issues, PRs, commits, reviews, comments, releases
- Webhooks auto-emitted per App installation

### Jira
- After OAuth, fetch `cloudId` from `/oauth/token/accessible-resources`
- Some streams require 1 HTTP call per issue — filter by project to reduce volume
- Webhooks available but delivery not guaranteed under load — use poll backstop
- Streams: issues, comments, changelogs, worklogs, sprints

### Notion
- **No webhook API** — polling only
- Rate limit: 3 req/sec per integration
- Cursor: `last_edited_time` on pages/databases
- Streams: pages, blocks, databases, comments, users
- Deeply nested block trees — flatten to plain text for Groq ingestion

### Zendesk
- Subdomain is per-customer — capture at OAuth connect time
- Rate limit: 700 req/min default
- For real-time: webhooks. For historical backfill: Incremental Export API (cursor-based)
- Streams: tickets, comments, users, organizations, articles (Help Center)

---

## Rate Limiting & Backoff

```python
# 1. Honor Retry-After / X-RateLimit-Reset headers first (+ 1s buffer)
# 2. Fallback: exponential backoff with full jitter
import random

def backoff(attempt: int, base: float = 1.0) -> float:
    return random.uniform(0, base * (2 ** attempt))

# 3. Proactive throttling for GitHub
if int(response.headers.get("X-RateLimit-Remaining", 9999)) < 50:
    sleep_until(int(response.headers["X-RateLimit-Reset"]))

# 4. Spread sweep start times across tenants
offset = hash(tenant_id) % 60
await asyncio.sleep(offset)
```

---

## Error Containment

### Error Taxonomy

| Type | Action |
|---|---|
| `ConfigError` (bad creds, revoked token) | Mark `status='auth_broken'`, surface to user, stop retrying |
| `TransientError` (429, 5xx, timeout) | Retry with exponential backoff + jitter, max 5 attempts |
| `SystemError` (connector bug, unexpected) | Log + alert ops, skip this connector, continue others |

### Circuit Breaker (per tenant × connector)

```
CLOSED ──(5 consecutive failures)──▶ OPEN
  ▲                                    │
  │                               (15min cooldown)
  │                                    ▼
  └──(probe succeeds)────────── HALF_OPEN
```

### Bulkhead

Each connector type gets a bounded `asyncio.Semaphore`. One misbehaving connector
(e.g., Notion timeouts) cannot consume all worker slots or block Slack/GitHub processing.

---

## Implementation Order (priority from KAN-2)

1. `BaseConnector` + `RawEvent` dataclass + `oauth.py` helpers
2. `/oauth/{source}/authorize` and `/oauth/{source}/callback` endpoints
3. **Notion** — polling connector (unblocks sweep worker first, no webhook complexity)
4. **GitHub** — App token auth + webhooks
5. **Jira** — 3LO OAuth + cloudId lookup + webhooks + poll backstop
6. **Slack** — Bot token + Events API webhooks
7. **Zendesk** — OAuth + Incremental Export for historical + webhooks
8. Context expanders (one per source — wraps connector to return enriched context for a given `source_id`)

---

## Dependencies to Add

```
dlt[postgres]>=0.4.0          # extraction library
dlt[slack]
dlt[github]
dlt[notion]
dlt[jira]
dlt[zendesk]
httpx>=0.27.0                 # async HTTP for OAuth exchange + API calls
cryptography>=42.0.0          # for token encryption helpers
```
