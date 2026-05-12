# company-brain-mvp

## Local services

- `brain-api`: `http://localhost:8080`
- FastMCP: `http://localhost:8001`
- Airbyte: see [airbyte/README.md](./airbyte/README.md)

## Quick start

```bash
cp .env.example .env
docker compose up -d postgres redis neo4j brain-api
curl -fsS http://localhost:8080/health
```

## Zendesk ingestion (Phase 2)

End-to-end runbook for tickets, comments, and ticket events normalized into
`raw_content` for downstream extraction and PM4Py process mining:
[`airbyte/zendesk-runbook.md`](./airbyte/zendesk-runbook.md).

Quick smoke test with the bundled fixture:

```bash
curl -sX POST http://localhost:8080/ingest/zendesk/sample | jq
curl -s   http://localhost:8080/ingest/process-mining/variants | jq
```
