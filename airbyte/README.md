# Airbyte Connector Setup

Airbyte OSS is self-hosted separately from the main Docker Compose stack. It runs via `abctl` (Airbyte CLI) and writes raw data to the `raw_content` table in Postgres.

## Install Airbyte

```bash
# Install abctl
curl -LsfS https://get.airbyte.com | bash

# Start Airbyte (separate from main compose)
abctl local install
```

Airbyte UI will be available at `http://localhost:8000` (note: conflicts with brain-api on port 8000 — run Airbyte on a different port or stop brain-api during setup).

## Source Connectors

### Zendesk Support

1. Go to Sources → New Source → Zendesk Support
2. Configure:
   - **Subdomain**: your Zendesk subdomain (e.g. `acme` for `acme.zendesk.com`)
   - **Authentication**: API token (Settings → Apps → Zendesk API → Token Access)
   - **Start date**: earliest date to sync
3. Select streams: `tickets`, `ticket_comments`, `ticket_events`, `ticket_fields`

### Slack

1. Go to Sources → New Source → Slack
2. Configure:
   - **API Token**: Bot token from Slack App settings (requires `channels:history`, `channels:read`, `users:read` scopes)
   - **Channel filter**: target channels for policy announcements (e.g. `#ops-announcements`, `#pricing-approvals`)
   - **Start date**: earliest date to sync

### Notion

1. Go to Sources → New Source → Notion
2. Configure:
   - **Token**: Notion integration token (create at https://www.notion.so/my-integrations)
   - Share relevant pages/databases with the integration

## Destination Connector

1. Go to Destinations → New Destination → PostgreSQL
2. Configure:
   - **Host**: `localhost` (or Docker host IP if Airbyte runs in Docker)
   - **Port**: `5432`
   - **Database**: `company_brain`
   - **Schema**: `public`
   - **Username**: `cb`
   - **Password**: `cb_secret`
3. Set the default stream prefix to ensure data lands in `raw_content`

> **Note**: Airbyte writes to its own staging tables by default. A custom destination mapping or a dbt transformation step is needed to normalize into `raw_content`. Configure the destination to write `source`, `source_id`, `content`, and `metadata` columns.

## Connections

Create one connection per source → destination pair:

| Connection | Sync frequency | Streams |
|------------|---------------|---------|
| Zendesk → Postgres | Hourly | tickets, ticket_comments, ticket_events |
| Slack → Postgres | Hourly | messages (target channels) |
| Notion → Postgres | Hourly | pages, databases |

## Verify

After first sync:

```sql
SELECT COUNT(*), source FROM raw_content GROUP BY source;
```

Expected output:
```
count | source
------+---------
  ... | zendesk
  ... | slack
  ... | notion
```
