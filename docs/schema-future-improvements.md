# Schema — Deferred Improvements

These are deliberate deferrals, not oversights. The core architecture problems (multi-tenancy, RBAC, auditability, secret handling, usage tracking, API access, tenant isolation) are solved. Revisit these when the product hits the relevant scale trigger.

---

## 1. Service Accounts

**Why:** n8n, LangGraph, CrewAI, MCP clients, and OpenAI Agents should not use a human user's credentials. Non-human actors need their own identity so API keys can be scoped, audited, and revoked independently.

**Proposed table:**
```sql
CREATE TABLE service_accounts (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id      UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  name        VARCHAR(100) NOT NULL,
  description TEXT,
  created_by  UUID NOT NULL REFERENCES users(id),
  created_at  TIMESTAMP DEFAULT NOW()
);
```

Link `organization_api_keys.created_by` → `service_accounts.id` (requires making that FK polymorphic or adding a `service_account_id` column).

**When to add:** When the first non-human integration (n8n, LangGraph) needs its own auditable identity.

---

## 2. Soft-Delete Strategy for Remaining Tables

`organizations` and `skills` have `deleted_at`. The following tables still have no explicit deletion strategy — decide intentionally before any data pipeline writes to them at volume:

| Table | Recommended strategy | Reason |
|-------|---------------------|--------|
| `source_connections` | `deleted_at` soft delete | OAuth credentials — may need to re-connect; don't lose config |
| `source_events` | Retain or archive to cold storage after 90 days | Can be very high volume; audit trail but not queried after processing |
| `review_queue` | Retain resolved items for 1 year, then archive | Audit trail for knowledge changes |
| `sweeps` | Retain forever (low volume, high value for debugging) | Sweep history is operationally useful |

**When to add:** Before first production data pipeline runs — once `source_events` starts filling up, a retention policy needs to exist.

---

## 3. Connector Accounts Split (`source_connections` → `connector_accounts` + config)

**Why:** `source_connections` currently mixes OAuth credential storage with connector configuration (monitored IDs, lookback days). As connectors grow, these concerns diverge:

```
Slack workspace
  ├── OAuth credentials (access_token_enc, refresh_token_enc, scopes)
  ├── webhook subscriptions
  ├── monitored channels
  ├── sync state (last_synced_at, cursor)
  └── rate-limit state (requests_remaining, reset_at)
```

**Proposed split:**
```sql
-- Pure credential storage
CREATE TABLE connector_accounts (
  id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id               UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  source               VARCHAR(50) NOT NULL,
  external_account_id  VARCHAR(255) NOT NULL,
  access_token_enc     BYTEA,
  refresh_token_enc    BYTEA,
  token_expires_at     TIMESTAMP,
  scopes               TEXT[],
  connected_by         UUID REFERENCES users(id),
  connected_at         TIMESTAMP DEFAULT NOW(),
  UNIQUE (org_id, source, external_account_id)
);

-- Per-channel/project configuration and sync state
CREATE TABLE connector_configs (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  connector_id      UUID NOT NULL REFERENCES connector_accounts(id) ON DELETE CASCADE,
  target_id         VARCHAR(255) NOT NULL,  -- channel / project / space ID
  lookback_days     INTEGER DEFAULT 180,
  last_synced_at    TIMESTAMP,
  sync_cursor       TEXT,                   -- provider-specific pagination cursor
  rate_limit_reset  TIMESTAMP,
  is_active         BOOLEAN DEFAULT TRUE
);
```

**When to add:** When a second monitored channel type per source is needed, or when sync state tracking becomes necessary for incremental fetches.
