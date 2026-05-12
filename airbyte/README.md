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

Airbyte should land raw sync data in Postgres first. The post-sync normalization
step that maps Airbyte's raw staging tables into the canonical `raw_content`
table lives in version-controlled code:

- contract: [`airbyte/raw-content-contract.md`](./raw-content-contract.md)
- Zendesk normalizer: [`brain-api/services/zendesk_normalizer.py`](../brain-api/services/zendesk_normalizer.py)
- Zendesk runbook: [`airbyte/zendesk-runbook.md`](./zendesk-runbook.md)

Do not try to normalize via the Airbyte UI destination mapping — that path is
brittle and not testable. Use the normalizer module instead.

## Detailed Connector Configuration

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

> **Note**: Airbyte writes to its own staging tables by default
> (e.g. `_airbyte_raw_tickets`, `_airbyte_raw_ticket_comments`,
> `_airbyte_raw_ticket_events`). The mapping from those staging tables into
> `raw_content` is done by `brain-api/services/zendesk_normalizer.py`. See
> [`airbyte/zendesk-runbook.md`](./zendesk-runbook.md) for the full flow.

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
