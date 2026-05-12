# Zendesk Ingestion Runbook

This runbook walks a teammate from a clean checkout to a verified Zendesk
ingestion flow: tickets, comments, and ticket events normalized into
`raw_content` and queryable as a PM4Py event log.

The flow:

```
Zendesk
  │
  │  Airbyte Source: Zendesk Support
  ▼
Airbyte staging tables (Postgres):
  _airbyte_raw_tickets
  _airbyte_raw_ticket_comments
  _airbyte_raw_ticket_events
  │
  │  brain-api/services/zendesk_normalizer.py
  │  (via POST /ingest/zendesk/* or the verify script)
  ▼
raw_content  ── UNIQUE (source, source_id) ──┐
  │                                          │
  │  brain-api/services/process_miner.py     │
  ▼                                          │
PM4Py event log (DataFrame)                  │
  │                                          │
  ▼                                          │
GET /ingest/process-mining/event-log ────────┘
GET /ingest/process-mining/variants
```

The normalizer enforces the contract from
[`raw-content-contract.md`](./raw-content-contract.md). Ticket events keep
their transition semantics (`field_name`, `previous_value`, `new_value`,
`source_timestamp`) in `metadata` so PM4Py can read them later without an
extra join.

---

## 1. Local prerequisites

```bash
brew install --cask docker          # Docker Desktop
brew tap airbytehq/tap && brew install abctl
```

Start Docker Desktop.

## 2. Repo setup

```bash
git clone <repo> && cd company-brain-mvp
cp .env.example .env
```

Fill the Zendesk values in `.env` (only required for the Airbyte connector,
not for brain-api itself):

```env
ZENDESK_SUBDOMAIN=acme
[email protected]
ZENDESK_API_TOKEN=zd-...
```

Generate the token at **Admin → Apps and integrations → Zendesk API → API
tokens**.

## 3. Bring up the app stack

```bash
docker compose up -d postgres redis neo4j brain-api
curl -fsS http://localhost:8080/health | jq
```

Health should return `{"status":"ok","db":true,"redis":true,"neo4j":true}`.

> **Existing volume?** The schema's `UNIQUE (source, source_id)` constraint
> only gets installed on a fresh Postgres volume (Docker init scripts run
> once). If `\d raw_content` doesn't show the constraint:
>
> ```bash
> docker compose exec -T postgres psql -U cb -d company_brain <<'SQL'
> ALTER TABLE raw_content
>   ADD CONSTRAINT raw_content_source_source_id_key UNIQUE (source, source_id);
> CREATE INDEX IF NOT EXISTS raw_content_source_idx ON raw_content (source);
> CREATE INDEX IF NOT EXISTS raw_content_entity_type_idx
>   ON raw_content (source, (metadata->>'entity_type'));
> CREATE INDEX IF NOT EXISTS raw_content_parent_idx
>   ON raw_content (source, (metadata->>'parent_source_id'));
> SQL
> ```

## 4. Verify the normalizer with the bundled fixture

Two equivalent paths.

### 4a. Through the running API (recommended)

```bash
curl -sX POST http://localhost:8080/ingest/zendesk/sample | jq
curl -s "http://localhost:8080/ingest/process-mining/event-log" | jq '.cases, .activities, (.rows | length)'
curl -s "http://localhost:8080/ingest/process-mining/variants" | jq
```

You should see two ticket cases (`ticket:12345`, `ticket:12346`) with the
status / priority / assignee / group activities discovered.

### 4b. Direct from the brain-api container

```bash
docker compose exec brain-api python scripts/verify_zendesk_ingestion.py --db
```

The script normalizes the fixture, validates each row against the contract,
upserts into `raw_content`, then prints the PM4Py event log and variants.

## 5. Wire up the real Zendesk connector (Airbyte)

Once the fixture flow passes end-to-end you can replace the fixture with
real data:

```bash
./airbyte/start.sh
abctl local credentials       # print Airbyte login
open http://localhost:8000
```

In the Airbyte UI:

1. **Sources → New → Zendesk Support**
   - Subdomain: `${ZENDESK_SUBDOMAIN}`
   - Auth: API token (email + token)
   - Start date: pick a few months back
   - Streams: `tickets`, `ticket_comments`, `ticket_events`, `ticket_fields`
2. **Destinations → New → PostgreSQL**
   - Host: `host.docker.internal` (so Airbyte's container can reach the
     compose Postgres on the host network)
   - Port: `5432`, Database: `company_brain`, User: `cb`, Password: `cb_secret`
   - Schema: `public`
3. **Connection** Zendesk → Postgres, hourly incremental sync.

Airbyte will write raw staging tables (e.g. `_airbyte_raw_tickets`). Then run
the normalizer over them. The MVP path is to fetch rows out of those tables
and POST them to `/ingest/zendesk` — that keeps all normalization in one
testable, version-controlled module. A scheduled driver job that does this
on each sync completion is a Phase 3 follow-up.

## 6. Endpoint reference

| Endpoint | Body | Purpose |
| --- | --- | --- |
| `POST /ingest/zendesk/tickets` | `[ZendeskTicket]` | Normalize + upsert tickets |
| `POST /ingest/zendesk/comments` | `[ZendeskComment]` | Normalize + upsert comments |
| `POST /ingest/zendesk/events` | `[ZendeskTicketEvent]` | Normalize + upsert events |
| `POST /ingest/zendesk` | `ZendeskBatchPayload` | Heterogeneous batch (tickets + comments + events) |
| `POST /ingest/zendesk/sample` | — | Load `brain-api/fixtures/zendesk_sample.json` |
| `GET /ingest/process-mining/event-log` | — | PM4Py-shaped event log (filter via `?ticket_source_id=ticket:N`) |
| `GET /ingest/process-mining/variants` | — | Variant counts over the event log |

Payload models are defined in `brain-api/models/schemas.py` (`ZendeskTicket`,
`ZendeskComment`, `ZendeskTicketEvent`, `ZendeskBatchPayload`). Each
permits extra fields, so passing the raw record from Airbyte / the Zendesk
API works without preprocessing.

## 7. Spot checks

```sql
-- contract checklist
SELECT metadata->>'entity_type' AS entity_type,
       COUNT(*) AS n
FROM raw_content
WHERE source = 'zendesk'
GROUP BY 1
ORDER BY 1;

-- preserved transition semantics on events
SELECT source_id,
       metadata->>'field_name'     AS field_name,
       metadata->>'previous_value' AS prev,
       metadata->>'new_value'      AS new,
       metadata->>'source_timestamp' AS ts
FROM raw_content
WHERE source = 'zendesk'
  AND metadata->>'entity_type' = 'ticket_event'
ORDER BY ts;
```

Every row should have non-null `metadata.entity_type`, `metadata.record_url`,
`metadata.author`, `metadata.created_at`, `metadata.updated_at`, and
`metadata.airbyte_stream`.

## 8. Troubleshooting

- **`ON CONFLICT` does nothing on re-ingest** — the `UNIQUE (source,
  source_id)` constraint is missing on an old Postgres volume. Apply the
  ALTER from step 3.
- **Events show up as `null:null` activities** — the source records didn't
  include `field_name` or `new_value`. Re-check the Airbyte mapping; the
  `ticket_events` stream must surface `field_name`, `previous_value`,
  `new_value`, and `created_at`.
- **Empty event log but tickets are present** — Airbyte's `ticket_events`
  stream wasn't enabled, or the connector version doesn't expose it. Fall
  back to unrolling `audits[].events` from the `ticket_audits` stream into
  individual `ZendeskTicketEvent` records.
