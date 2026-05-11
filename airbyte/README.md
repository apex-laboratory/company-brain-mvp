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
