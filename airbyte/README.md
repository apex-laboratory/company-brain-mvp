# Airbyte Local Setup

Airbyte runs outside the main `docker compose` stack and is used here for connector infrastructure, not for any product UI work. The stable local setup is:

- Airbyte: `http://localhost:8000`
- `brain-api`: `http://localhost:8080`
- FastMCP: `http://localhost:8001`

This avoids the old `8000`/`8000` port collision between Airbyte and `brain-api`.

## Why Airbyte

The PRD calls for Airbyte OSS as the connector layer and a separate local runtime managed with `abctl`. Airbyte's current docs say self-managed Airbyte supports over 600 sources and destinations, which covers the MVP sources in the PRD and gives us room to add more later.

I also checked Meltano Hub as the main fallback catalog. It is useful when we need a connector Airbyte does not ship directly because it includes Singer taps and Airbyte-wrapper variants, but it is not a drop-in replacement for this ticket's local Airbyte runtime.

## Prerequisites

1. Install Docker Desktop and make sure it is running.
2. Install `abctl`.

```bash
brew tap airbytehq/tap
brew install abctl
```

3. Copy the project env file if you have not already:

```bash
cp .env.example .env
```

## Port Convention

We keep Airbyte on its default port and move only the host-facing app port:

- Airbyte stays on `8000`
- `brain-api` is exposed on `8080`

That means teammates can keep following the standard Airbyte quickstart without having to remember a custom UI port.

## Start The Database Destination

For connector work, the only required service from the main repo is Postgres:

```bash
docker compose up -d postgres
```

Verify:

```bash
docker compose ps postgres
```

## Optional: Start The Full App Stack

If you also want the rest of the local app running:

From the repo root:

```bash
docker compose up -d postgres redis neo4j brain-api
```

Verify:

```bash
curl http://localhost:8080/health
```

## Start Airbyte

From the repo root:

```bash
./airbyte/start.sh
```

What this does:

- checks that Docker is running
- checks that `abctl` is installed
- installs or updates local Airbyte with `abctl local install --port 8000`
- keeps Airbyte on `http://localhost:8000`

After startup, fetch credentials with:

```bash
abctl local credentials
```

Check status any time with:

```bash
./airbyte/status.sh
```

## Shut Down Airbyte

```bash
./airbyte/stop.sh
```

This runs `abctl local uninstall`.

Important:

- this removes the local Airbyte deployment
- persisted Airbyte data is kept by default
- do not pass `--persisted` unless you intentionally want to wipe Airbyte data

## Connector Scope

The Phase 2 PRD expects these Airbyte source connectors:

- Zendesk Support
- Slack
- Notion

And this destination:

- PostgreSQL

Airbyte should land raw sync data in Postgres first. If we later need normalized writes into `raw_content`, we should handle that in a follow-up transformation layer instead of relying on manual UI mapping.

The canonical normalization contract lives in [airbyte/raw-content-contract.md](/Users/afnan/company-brain-mvp/airbyte/raw-content-contract.md).

## Detailed Connector Configuration

### Zendesk Support

1. Go to Sources → New Source → Zendesk Support
2. Configure:
   - **Subdomain**: your Zendesk subdomain (e.g. `acme` for `acme.zendesk.com`)
   - **Authentication**: API token (Settings → Apps → Zendesk API → Token Access)
   - **Start date**: earliest date to sync
3. Select streams: `tickets`, `ticket_comments`, `ticket_events`, `ticket_fields`

### Slack (L1-04)

1. Go to Sources → New Source → Slack
2. Configure:
   - **API Token**: Bot token from Slack App settings (requires `channels:history`, `channels:read`, `users:read` scopes; add `groups:history` / `groups:read` for private channels)
   - **Channel filter**: scope to high-signal, policy-heavy channels (e.g. `#ops-announcements`, `#pricing-approvals`). Avoid social channels — they add noise without lifting rule recall.
   - **Start date**: earliest date to backfill
3. Select streams: `channels`, `users`, `channel_messages`, `threads`. `channels` and `users` are required for resolving channel names and `@user` mentions during normalization.
4. Connect to the Postgres destination (see Destination section). Run "Sync Now" for the historical backfill and confirm Airbyte reports success.
5. Run the post-sync normalization step:

   ```bash
   export DATABASE_URL=postgresql://cb:cb_secret@localhost:5432/company_brain
   export SLACK_WORKSPACE=your-workspace        # subdomain only, no .slack.com
   python -m airbyte.normalize.run slack
   ```

   This reads Airbyte's raw staging tables (`airbyte_internal.slack_raw__stream_*`
   or the legacy `_airbyte_raw_*` layout) and writes canonical rows into
   `raw_content` per [airbyte/raw-content-contract.md](raw-content-contract.md).
   Channel-join / topic-change / bot-add system messages are dropped as noise.

6. Verify (L1-04 acceptance criteria):

   ```sql
   SELECT COUNT(*) FROM raw_content WHERE source = 'slack';
   SELECT source_id,
          left(content, 80)              AS preview,
          metadata->>'entity_type'        AS entity_type,
          metadata->>'channel_name'       AS channel,
          metadata->'author'->>'handle'   AS author
   FROM raw_content
   WHERE source = 'slack'
   LIMIT 5;
   ```

### Notion

1. Go to Sources → New Source → Notion
2. Configure:
   - **Token**: Notion integration token (create at https://www.notion.so/my-integrations)
   - Share relevant pages/databases with the integration

### GitHub

1. Go to Sources → New Source → GitHub
2. Configure:
   - **Authentication**: personal access token or GitHub App credentials
   - **Repositories**: target repositories with operational knowledge
   - **Start date**: earliest date to sync
3. Select streams: `issues`, `pull_requests`, `issue_comments`, `pull_request_comments`

### Jira

1. Go to Sources → New Source → Jira
2. Configure:
   - **Base URL**: your Jira workspace URL
   - **Authentication**: API token or OAuth credentials
   - **Projects / JQL filter**: target projects with support, ops, or engineering workflow knowledge
   - **Start date**: earliest date to sync
3. Select streams: `issues`, `comments`, `worklogs`, and transition history if exposed by the connector

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
| GitHub → Postgres | Hourly | issues, pull_requests, comments |
| Jira → Postgres | Hourly | issues, comments, worklogs, transitions |

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
  ... | github
  ... | jira
```

## Teammate Workflow

1. Start Docker Desktop.
2. Run `docker compose up -d postgres`.
3. Run `./airbyte/start.sh`.
4. Open Airbyte at `http://localhost:8000`.
5. Run `abctl local credentials` if login details are needed.
6. If you also need the app locally, run `docker compose up -d redis neo4j brain-api` and confirm `brain-api` is healthy at `http://localhost:8080/health`.
7. When done, run `./airbyte/stop.sh`.

## Troubleshooting

### `abctl: command not found`

Install it with Homebrew:

```bash
brew tap airbytehq/tap
brew install abctl
```

### Docker connection errors

If you see errors about the Docker socket, Docker Desktop is not running yet. Start Docker Desktop first, then retry.

### Port already in use

Check what is bound to `8000` or `8080`:

```bash
lsof -iTCP:8000 -sTCP:LISTEN
lsof -iTCP:8080 -sTCP:LISTEN
```

If `brain-api` is still on `8000`, recreate the compose stack so it picks up the new port mapping:

```bash
docker compose down
docker compose up -d postgres redis neo4j brain-api
```
