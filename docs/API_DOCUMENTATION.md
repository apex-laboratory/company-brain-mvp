# Brainite API Documentation

This document describes the backend API expected by the current Brainite V2 frontend prototype. The UI is currently static and mock-driven, so these contracts are inferred from the screens, fixture data, and the planned FastAPI backend shape.

## Backend Shape

Recommended stack:

- Python 3.12+ + FastAPI
- PostgreSQL + SQLAlchemy 2.0 async + Alembic
- Pydantic v2 validation
- JWT access tokens (self-issued; no Supabase dependency)
- Read-only OAuth integrations
- OpenAI / Anthropic behind an internal provider abstraction
- Redis + ARQ for sync, ingestion, extraction, and AI jobs

Recommended API module layout:

```txt
app/
  main.py          # FastAPI app factory, lifespan, middleware registration
  config.py        # pydantic_settings.BaseSettings — all env vars
  modules/
    auth/
    users/
    companies/
    sources/
    decisions/
    reviews/
    skills/
    brain/
    usage/
  integrations/
    openai.py
    anthropic.py
    slack.py
    notion.py
    github.py
    jira.py
    zendesk.py
    prompts/       # AI prompt templates
  models/
    orm/           # SQLAlchemy mapped classes
    schemas/       # Pydantic request/response models
  shared/
    middleware/
    helpers/
    constants/
    logger/        # structlog JSON logging
  jobs/            # ARQ worker and task definitions
alembic/
  versions/        # migration files
tests/
```

Routers should only handle HTTP request/response concerns. Services should own business logic. Repositories should own database queries. Integrations should isolate external SDKs and credentials.

## Base URLs

```txt
Production API: https://api.Brainite.com/api/v1
Workspace MCP: https://{workspaceSlug}.Brainite.com/mcp
Local API:      http://localhost:4000/api/v1
```

All JSON endpoints use:

```http
Content-Type: application/json
Accept: application/json
```

All REST routes in this document are written relative to the API version prefix:

```txt
/api/v1
```

For example:

```txt
POST /api/v1/auth/signin
GET  /api/v1/workspaces/:workspaceId/decisions
POST /api/v1/workspaces/:workspaceId/brain/query
```

## API Route Naming Convention

Use predictable, resource-oriented route names:

| Rule               | Convention                                                   | Example                                                     |
| ------------------ | ------------------------------------------------------------ | ----------------------------------------------------------- |
| Version prefix     | Always mount REST APIs under `/api/v1`                       | `/api/v1/auth/signin`                                       |
| Auth routes        | Use `/auth` because auth is an action domain                 | `/api/v1/auth/signup`                                       |
| Workspace data     | Scope product data under `/workspaces/:workspaceId`          | `/api/v1/workspaces/:workspaceId/sources`                   |
| Collections        | Use plural nouns for resources                               | `/api/v1/workspaces/:workspaceId/decisions`                 |
| Single resources   | Use `/:resourceId` for one item                              | `/api/v1/workspaces/:workspaceId/skills/:skillId`           |
| Actions            | Use verb subroutes only when the operation is not plain CRUD | `/api/v1/workspaces/:workspaceId/reviews/:reviewId/resolve` |
| Background jobs    | Use nouns for job collections, status by ID                  | `/api/v1/workspaces/:workspaceId/brain/builds/:buildId`     |
| Provider callbacks | Keep provider in the path for OAuth clarity                  | `/api/v1/workspaces/:workspaceId/sources/slack/callback`    |
| Server webhooks    | Keep provider webhooks outside workspace routes              | `/api/v1/webhooks/github`                                   |
| MCP endpoint       | Keep MCP separate from REST versioning                       | `https://{workspaceSlug}.Brainite.com/mcp`                  |

Naming style:

- Use lowercase kebab-case for multi-word path segments: `/api-keys`, `/source-items`.
- Use camelCase for JSON fields: `workspaceId`, `accessToken`, `sourceProvider`.
- Use stable opaque IDs with readable prefixes: `usr_`, `wrk_`, `src_`, `dec_`, `rev_`, `skl_`.
- Avoid verbs for normal CRUD routes. Prefer `GET /decisions/:decisionId` over `GET /getDecision`.
- Keep query params for filtering, sorting, pagination, and search: `?status=approved&limit=25`.

## Authentication

The dashboard API uses a short-lived JWT access token. Agent/MCP access uses a workspace API key.

### Dashboard Bearer Token

```http
Authorization: Bearer <accessToken>
```

Use this for browser app requests after sign in.

### Workspace API Key

```http
Authorization: Bearer hph_live_xxxxxxxxxxxxxxxxx
```

Use this for external agents, MCP clients, and server-to-server brain queries. Store hashed API keys in the database. Show the raw key once at creation time.

### OAuth State

OAuth connection endpoints should generate a signed, short-lived `state` value. The callback must verify:

- state signature
- state expiration
- current user/workspace ownership
- requested provider
- redirect URI

## API Keys And Secrets

These keys are associated with backend requests. None should be exposed to the frontend.

| Key                                           | Used By                     | Purpose                                     | Where Stored  |
| --------------------------------------------- | --------------------------- | ------------------------------------------- | ------------- |
| `JWT_ACCESS_SECRET`                           | API                         | Sign dashboard access tokens                | backend env   |
| `JWT_REFRESH_SECRET`                          | API                         | Sign refresh tokens                         | backend env   |
| `OPENAI_API_KEY`                              | `integrations/openai.py`    | Brain answers, extraction, skill generation | backend env   |
| `ANTHROPIC_API_KEY`                           | `integrations/anthropic.py` | Optional alternate AI provider              | backend env   |
| `SLACK_CLIENT_ID` / `SLACK_CLIENT_SECRET`     | `integrations/slack.py`     | Slack OAuth                                 | backend env   |
| `NOTION_CLIENT_ID` / `NOTION_CLIENT_SECRET`   | `integrations/notion.py`    | Notion OAuth                                | backend env   |
| `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET`   | `integrations/github.py`    | GitHub OAuth                                | backend env   |
| `JIRA_CLIENT_ID` / `JIRA_CLIENT_SECRET`       | `integrations/jira.py`      | Jira OAuth                                  | backend env   |
| `ZENDESK_CLIENT_ID` / `ZENDESK_CLIENT_SECRET` | `integrations/zendesk.py`   | Zendesk OAuth                               | backend env   |
| `RESEND_API_KEY`                              | email job                   | Invites, auth emails, alerts                | backend env   |
| `REDIS_URL`                                   | jobs/cache                  | ARQ jobs and rate limits                    | backend env   |
| `DATABASE_URL`                                | SQLAlchemy                  | PostgreSQL connection (`postgresql+asyncpg://`) | backend env |
| `WORKSPACE_API_KEY_HASH`                      | database                    | Agent/MCP authentication                    | database only |

## Standard Response Envelope

Successful responses:

```json
{
  "data": {},
  "meta": {
    "requestId": "req_01HZ...",
    "timestamp": "2026-06-04T10:22:31.000Z"
  }
}
```

Validation or application errors:

```json
{
  "error": {
    "code": "validation_error",
    "message": "Request body is invalid.",
    "details": [
      {
        "path": "email",
        "message": "Invalid email address"
      }
    ]
  },
  "meta": {
    "requestId": "req_01HZ..."
  }
}
```

Common status codes:

| Status | Meaning                            |
| ------ | ---------------------------------- |
| `200`  | Success                            |
| `201`  | Created                            |
| `202`  | Accepted for background processing |
| `204`  | Success with no body               |
| `400`  | Bad request                        |
| `401`  | Missing or invalid auth            |
| `403`  | Authenticated but not allowed      |
| `404`  | Resource not found                 |
| `409`  | Conflict                           |
| `422`  | Validation failed                  |
| `429`  | Rate limited                       |
| `500`  | Server error                       |

## Core Types

### User

```json
{
  "id": "usr_123",
  "name": "Dana Reyes",
  "email": "dana@riverline.io",
  "role": "admin",
  "title": "Head of CX",
  "avatarColor": "#C2603A"
}
```

### Workspace

```json
{
  "id": "wrk_123",
  "name": "Riverline",
  "slug": "riverline",
  "domain": "riverline.io",
  "plan": "pro",
  "seatLimit": 12,
  "memberCount": 4
}
```

### Source

```json
{
  "id": "src_123",
  "provider": "slack",
  "name": "Slack",
  "status": "connected",
  "syncStatus": "healthy",
  "lastSyncedAt": "2026-06-04T10:18:00.000Z",
  "health": 98,
  "pendingItems": 3,
  "activeChannelCount": 18,
  "extractedLabel": "184 decisions"
}
```

Allowed source providers:

```txt
slack, notion, github, jira, zendesk, google_drive
```

### Decision

```json
{
  "id": "dec_123",
  "title": "Premium refund exception",
  "sourceProvider": "notion",
  "sourceLocation": "Policy Library",
  "status": "approved",
  "confidence": 96,
  "category": "Support",
  "owner": {
    "name": "Dana Reyes",
    "avatarColor": "#C2603A"
  },
  "monthlyUses": 2100,
  "updatedAt": "2026-06-02T10:00:00.000Z",
  "summary": "Premium-tier customers receive a 45-day refund window.",
  "rule": "IF customer.tier = premium AND days_since_purchase <= 45 THEN approve_refund() ELSE require_manager_approval()"
}
```

Allowed decision statuses:

```txt
approved, active, review
```

### Review

```json
{
  "id": "rev_123",
  "title": "Refund window extended for premium tier",
  "kind": "policy_change",
  "sourceProvider": "slack",
  "sourceLocation": "#cs-escalations - 14 messages",
  "before": "Premium refund window: 30 days",
  "after": "Premium refund window: 45 days",
  "evidenceQuote": "Let's just give premium folks 45 days...",
  "evidenceAuthor": "Dana R.",
  "confidence": 92,
  "status": "pending"
}
```

Allowed review verdicts:

```txt
approve, reject
```

### Skill

```json
{
  "id": "skl_123",
  "name": "premium-refund-policy",
  "version": "v4",
  "sourceProviders": ["notion", "slack"],
  "calls30d": 2100,
  "status": "stable",
  "updatedAt": "2026-06-02T10:00:00.000Z"
}
```

Allowed skill statuses:

```txt
stable, active, draft, review
```

## Auth API

### Create Email Signup

```http
POST /auth/signup
```

Request:

```json
{
  "email": "dana@riverline.io"
}
```

Response `201`:

```json
{
  "data": {
    "user": {
      "id": "usr_123",
      "email": "dana@riverline.io",
      "name": null
    },
    "workspace": null,
    "accessToken": "eyJ...",
    "refreshToken": "eyJ...",
    "nextStep": "onboarding"
  }
}
```

### Email Sign In

```http
POST /auth/signin
```

Request:

```json
{
  "email": "dana@riverline.io"
}
```

Response `200`:

```json
{
  "data": {
    "user": {
      "id": "usr_123",
      "email": "dana@riverline.io",
      "name": "Dana Reyes"
    },
    "workspace": {
      "id": "wrk_123",
      "name": "Riverline",
      "slug": "riverline"
    },
    "accessToken": "eyJ...",
    "refreshToken": "eyJ...",
    "nextStep": "dashboard"
  }
}
```

### Start OAuth

```http
GET /auth/oauth/:provider/start?mode=signup
```

Path params:

```txt
provider: google, github, saml
```

Response `200`:

```json
{
  "data": {
    "authorizationUrl": "https://accounts.google.com/o/oauth2/v2/auth?...",
    "state": "st_..."
  }
}
```

### OAuth Callback

```http
POST /auth/oauth/:provider/callback
```

Request:

```json
{
  "code": "oauth_code",
  "state": "st_..."
}
```

Response `200`:

```json
{
  "data": {
    "user": {},
    "workspace": {},
    "accessToken": "eyJ...",
    "refreshToken": "eyJ...",
    "nextStep": "dashboard"
  }
}
```

### Refresh Token

```http
POST /auth/refresh
```

Request:

```json
{
  "refreshToken": "eyJ..."
}
```

Response `200`:

```json
{
  "data": {
    "accessToken": "eyJ...",
    "refreshToken": "eyJ..."
  }
}
```

### Logout

```http
POST /auth/logout
```

Request:

```json
{
  "refreshToken": "eyJ..."
}
```

Response `204`: no body.

## Onboarding API

### Create Workspace

```http
POST /workspaces
Authorization: Bearer <accessToken>
```

Request:

```json
{
  "companyName": "Riverline",
  "teamSize": "51-200",
  "primaryUseCase": "support"
}
```

Response `201`:

```json
{
  "data": {
    "workspace": {
      "id": "wrk_123",
      "name": "Riverline",
      "slug": "riverline",
      "plan": "trial"
    }
  }
}
```

Allowed `teamSize` values:

```txt
1-10, 11-50, 51-200, 200+
```

Allowed `primaryUseCase` values:

```txt
support, ops, eng, agents
```

### Save Onboarding Progress

```http
PATCH /workspaces/:workspaceId/onboarding
Authorization: Bearer <accessToken>
```

Request:

```json
{
  "step": "configure",
  "companyName": "Riverline",
  "teamSize": "51-200",
  "primaryUseCase": "support",
  "connectedProviders": ["slack", "notion"],
  "timeRange": "90d",
  "channels": {
    "slack": ["#cs-escalations", "#incidents", "#refunds-policy"],
    "notion": ["Policy Library", "Support Playbook"]
  }
}
```

Response `200`:

```json
{
  "data": {
    "status": "saved",
    "nextStep": "build"
  }
}
```

Allowed `timeRange` values:

```txt
30d, 90d, 6mo, all
```

## Source Integrations API

### List Source Providers

```http
GET /sources/providers
Authorization: Bearer <accessToken>
```

Response `200`:

```json
{
  "data": [
    {
      "provider": "slack",
      "name": "Slack",
      "tag": "Conversations & decisions",
      "estimatedItems": "3,412 messages",
      "defaultScopes": ["channels:history", "channels:read"],
      "readOnly": true
    },
    {
      "provider": "notion",
      "name": "Notion",
      "tag": "Policies & playbooks",
      "estimatedItems": "284 pages",
      "defaultScopes": ["read_content"],
      "readOnly": true
    },
    {
      "provider": "google_drive",
      "name": "Google Drive",
      "tag": "SOPs & runbooks",
      "estimatedItems": "1,204 files",
      "defaultScopes": ["https://www.googleapis.com/auth/drive.readonly"],
      "readOnly": true
    }
  ]
}
```

### Start Source OAuth

```http
POST /workspaces/:workspaceId/sources/:provider/connect
Authorization: Bearer <accessToken>
```

Request:

```json
{
  "redirectUri": "https://app.Brainite.com/oauth/callback",
  "requestedScopes": ["read"]
}
```

Response `200`:

```json
{
  "data": {
    "authorizationUrl": "https://slack.com/oauth/v2/authorize?...",
    "state": "st_..."
  }
}
```

### Source OAuth Callback

```http
POST /workspaces/:workspaceId/sources/:provider/callback
Authorization: Bearer <accessToken>
```

Request:

```json
{
  "code": "provider_oauth_code",
  "state": "st_..."
}
```

Response `201`:

```json
{
  "data": {
    "source": {
      "id": "src_123",
      "provider": "slack",
      "name": "Slack",
      "status": "connected",
      "syncStatus": "pending",
      "lastSyncedAt": null
    }
  }
}
```

### List Connected Sources

```http
GET /workspaces/:workspaceId/sources
Authorization: Bearer <accessToken>
```

Response `200`:

```json
{
  "data": [
    {
      "id": "src_123",
      "provider": "slack",
      "name": "Slack",
      "status": "connected",
      "syncStatus": "healthy",
      "lastSyncedAt": "2026-06-04T10:18:00.000Z",
      "health": 98,
      "pendingItems": 3,
      "activeChannelCount": 18,
      "extractedLabel": "184 decisions",
      "ingest7d": [20, 28, 24, 32, 30, 38, 42]
    }
  ]
}
```

### List Source Channels

```http
GET /workspaces/:workspaceId/sources/:sourceId/channels
Authorization: Bearer <accessToken>
```

Response `200`:

```json
{
  "data": [
    {
      "id": "chn_123",
      "name": "#cs-escalations",
      "provider": "slack",
      "selected": true,
      "itemCount": 880
    },
    {
      "id": "chn_124",
      "name": "#incidents",
      "provider": "slack",
      "selected": true,
      "itemCount": 420
    }
  ]
}
```

### Configure Source Scope

```http
PUT /workspaces/:workspaceId/sources/scope
Authorization: Bearer <accessToken>
```

Request:

```json
{
  "timeRange": "90d",
  "channels": {
    "slack": ["#cs-escalations", "#incidents", "#refunds-policy"],
    "notion": ["Policy Library", "Support Playbook", "Ops Runbooks"],
    "github": ["payments-core", "dispute-engine"],
    "jira": ["Incident Response", "Platform"],
    "zendesk": ["Escalations", "Disputes"]
  }
}
```

Response `200`:

```json
{
  "data": {
    "status": "configured",
    "estimatedDecisions": 378
  }
}
```

### Disconnect Source

```http
DELETE /workspaces/:workspaceId/sources/:sourceId
Authorization: Bearer <accessToken>
```

Response `204`: no body.

## Brain Build API

### Start Brain Build

```http
POST /workspaces/:workspaceId/brain/builds
Authorization: Bearer <accessToken>
```

Request:

```json
{
  "timeRange": "90d",
  "sourceIds": ["src_slack", "src_notion", "src_github"],
  "extract": ["decisions", "policies", "skills"]
}
```

Response `202`:

```json
{
  "data": {
    "buildId": "bld_123",
    "status": "queued",
    "progress": 0
  }
}
```

### Get Brain Build Status

```http
GET /workspaces/:workspaceId/brain/builds/:buildId
Authorization: Bearer <accessToken>
```

Response `200`:

```json
{
  "data": {
    "buildId": "bld_123",
    "status": "running",
    "progress": 62,
    "currentStep": "extract_decisions",
    "counts": {
      "sourcesRead": 5,
      "decisionsExtracted": 184,
      "policiesExtracted": 52,
      "skillsGenerated": 37,
      "reviewItemsCreated": 3
    }
  }
}
```

Allowed build statuses:

```txt
queued, running, completed, failed, canceled
```

### Cancel Brain Build

```http
POST /workspaces/:workspaceId/brain/builds/:buildId/cancel
Authorization: Bearer <accessToken>
```

Response `200`:

```json
{
  "data": {
    "buildId": "bld_123",
    "status": "canceled"
  }
}
```

## Dashboard API

### Get Overview

```http
GET /workspaces/:workspaceId/overview
Authorization: Bearer <accessToken>
```

Response `200`:

```json
{
  "data": {
    "workspace": {
      "name": "Riverline",
      "slug": "riverline",
      "plan": "pro"
    },
    "greetingName": "Dana",
    "sync": {
      "status": "healthy",
      "label": "All sources synced",
      "lastSyncedAt": "2026-06-04T10:18:00.000Z"
    },
    "kpis": [
      {
        "id": "decisions",
        "label": "Decisions",
        "value": 184,
        "trend": 8,
        "spark": [120, 138, 150, 162, 170, 178, 184]
      },
      {
        "id": "policies",
        "label": "Policies",
        "value": 52,
        "trend": 6,
        "spark": [30, 36, 40, 44, 46, 49, 52]
      },
      {
        "id": "skills",
        "label": "Skills live",
        "value": 37,
        "trend": 12,
        "spark": [12, 18, 22, 26, 30, 34, 37]
      },
      {
        "id": "reviews",
        "label": "Awaiting review",
        "value": 3,
        "trend": -25,
        "spark": [6, 5, 7, 4, 5, 4, 3]
      }
    ],
    "recentQuestions": [
      "How do enterprise discounts get approved?",
      "What happens when a shipment arrives damaged?",
      "When should incidents be escalated to engineering?"
    ],
    "reviewPreview": [],
    "recentDecisions": [],
    "sourceHealth": [],
    "activity": []
  }
}
```

### Get Activity Feed

```http
GET /workspaces/:workspaceId/activity?limit=20
Authorization: Bearer <accessToken>
```

Response `200`:

```json
{
  "data": [
    {
      "id": "act_123",
      "type": "skill",
      "title": "New skill proposed",
      "detail": "chargeback-triage v3",
      "sourceProvider": "github",
      "createdAt": "2026-06-04T10:10:00.000Z"
    }
  ],
  "meta": {
    "nextCursor": "act_122"
  }
}
```

## Brain Query API

### Ask Brain

```http
POST /workspaces/:workspaceId/brain/query
Authorization: Bearer <accessToken or workspaceApiKey>
```

Request:

```json
{
  "question": "What's our refund policy for premium customers?",
  "conversationId": "cnv_123",
  "includeSources": true,
  "maxSources": 5
}
```

Response `200`:

```json
{
  "data": {
    "conversationId": "cnv_123",
    "messageId": "msg_456",
    "answer": "Premium customers have a 45-day refund window. Past 45 days, refunds need manager approval in #cs-escalations. Damaged-item claims under $200 auto-issue a replacement.",
    "confidence": 96,
    "sources": [
      {
        "provider": "notion",
        "label": "Policy Library",
        "sourceItemId": "itm_123",
        "url": "https://notion.so/...",
        "excerpt": "Premium-tier customers receive a 45-day refund window."
      },
      {
        "provider": "slack",
        "label": "#cs-escalations",
        "sourceItemId": "itm_456",
        "url": "https://riverline.slack.com/archives/...",
        "excerpt": "Premium folks get 45 days..."
      }
    ]
  }
}
```

### List Brain Conversations

```http
GET /workspaces/:workspaceId/brain/conversations
Authorization: Bearer <accessToken>
```

Response `200`:

```json
{
  "data": [
    {
      "id": "cnv_123",
      "title": "Refund policy for premium customers",
      "updatedAt": "2026-06-04T10:22:31.000Z"
    }
  ]
}
```

### Get Brain Conversation

```http
GET /workspaces/:workspaceId/brain/conversations/:conversationId
Authorization: Bearer <accessToken>
```

Response `200`:

```json
{
  "data": {
    "id": "cnv_123",
    "messages": [
      {
        "id": "msg_1",
        "role": "user",
        "content": "What's our refund policy for premium customers?",
        "createdAt": "2026-06-04T10:22:00.000Z"
      },
      {
        "id": "msg_2",
        "role": "assistant",
        "content": "Premium customers have a 45-day refund window...",
        "confidence": 96,
        "sources": [],
        "createdAt": "2026-06-04T10:22:31.000Z"
      }
    ]
  }
}
```

## Decisions API

### List Decisions

```http
GET /workspaces/:workspaceId/decisions?status=all&category=Support&cursor=&limit=25
Authorization: Bearer <accessToken>
```

Query params:

| Param            | Type   | Notes                                    |
| ---------------- | ------ | ---------------------------------------- |
| `status`         | string | `all`, `approved`, `active`, or `review` |
| `category`       | string | Optional category filter                 |
| `sourceProvider` | string | Optional provider filter                 |
| `search`         | string | Optional title/body search               |
| `cursor`         | string | Cursor pagination                        |
| `limit`          | number | Default `25`, max `100`                  |

Response `200`:

```json
{
  "data": [
    {
      "id": "dec_123",
      "title": "Premium refund exception",
      "sourceProvider": "notion",
      "sourceLocation": "Policy Library",
      "status": "approved",
      "confidence": 96,
      "category": "Support",
      "owner": {
        "name": "Dana Reyes",
        "avatarColor": "#C2603A"
      },
      "monthlyUses": 2100,
      "updatedAt": "2026-06-02T10:00:00.000Z"
    }
  ],
  "meta": {
    "nextCursor": null
  }
}
```

### Get Decision

```http
GET /workspaces/:workspaceId/decisions/:decisionId
Authorization: Bearer <accessToken>
```

Response `200`:

```json
{
  "data": {
    "id": "dec_123",
    "title": "Premium refund exception",
    "sourceProvider": "notion",
    "sourceLocation": "Policy Library",
    "status": "approved",
    "confidence": 96,
    "category": "Support",
    "owner": {
      "name": "Dana Reyes",
      "avatarColor": "#C2603A"
    },
    "monthlyUses": 2100,
    "summary": "Premium-tier customers receive a 45-day refund window. Beyond 45 days, refunds require manager approval.",
    "rule": "IF customer.tier = premium AND days_since_purchase <= 45 THEN approve_refund() ELSE require_manager_approval()",
    "provenance": {
      "sourceProvider": "notion",
      "sourceLocation": "Policy Library",
      "sourceItemId": "itm_123",
      "url": "https://notion.so/...",
      "extractedAt": "2026-06-02T10:00:00.000Z"
    },
    "updatedAt": "2026-06-02T10:00:00.000Z"
  }
}
```

### Pin Decision

```http
POST /workspaces/:workspaceId/decisions/:decisionId/pin
Authorization: Bearer <accessToken>
```

Response `200`:

```json
{
  "data": {
    "id": "dec_123",
    "pinned": true
  }
}
```

## Reviews API

### List Review Queue

```http
GET /workspaces/:workspaceId/reviews?status=pending
Authorization: Bearer <accessToken>
```

Response `200`:

```json
{
  "data": [
    {
      "id": "rev_123",
      "title": "Refund window extended for premium tier",
      "kind": "policy_change",
      "sourceProvider": "slack",
      "sourceLocation": "#cs-escalations - 14 messages",
      "before": "Premium refund window: 30 days",
      "after": "Premium refund window: 45 days",
      "evidenceQuote": "Let's just give premium folks 45 days...",
      "evidenceAuthor": "Dana R. - Head of CX",
      "confidence": 92,
      "status": "pending"
    }
  ],
  "meta": {
    "total": 3,
    "completedToday": 0
  }
}
```

### Resolve Review

```http
POST /workspaces/:workspaceId/reviews/:reviewId/resolve
Authorization: Bearer <accessToken>
```

Request:

```json
{
  "verdict": "approve",
  "comment": "Looks right."
}
```

Response `200`:

```json
{
  "data": {
    "id": "rev_123",
    "status": "approved",
    "mergedIntoBrain": true,
    "decisionId": "dec_789",
    "skillId": null
  }
}
```

If rejected:

```json
{
  "data": {
    "id": "rev_123",
    "status": "rejected",
    "mergedIntoBrain": false
  }
}
```

## Skills API

### List Skills

```http
GET /workspaces/:workspaceId/skills?search=refund&status=stable&limit=25
Authorization: Bearer <accessToken>
```

Response `200`:

```json
{
  "data": [
    {
      "id": "skl_123",
      "name": "premium-refund-policy",
      "version": "v4",
      "sourceProviders": ["notion", "slack"],
      "calls30d": 2100,
      "status": "stable",
      "spark": [120, 160, 180, 210, 240, 260, 290],
      "updatedAt": "2026-06-02T10:00:00.000Z"
    }
  ],
  "meta": {
    "stats": {
      "totalSkills": 37,
      "stable": 31,
      "inReview": 4,
      "calls30d": 11600
    },
    "nextCursor": null
  }
}
```

### Get Skill

```http
GET /workspaces/:workspaceId/skills/:skillId
Authorization: Bearer <accessToken>
```

Response `200`:

```json
{
  "data": {
    "id": "skl_123",
    "name": "premium-refund-policy",
    "version": "v4",
    "description": "Determines refund eligibility for premium customers.",
    "inputSchema": {
      "type": "object",
      "required": ["customerTier", "daysSincePurchase"],
      "properties": {
        "customerTier": {
          "type": "string"
        },
        "daysSincePurchase": {
          "type": "number"
        }
      }
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "action": {
          "type": "string"
        },
        "requiresApproval": {
          "type": "boolean"
        }
      }
    },
    "sourceProviders": ["notion", "slack"],
    "status": "stable",
    "createdAt": "2026-05-10T10:00:00.000Z",
    "updatedAt": "2026-06-02T10:00:00.000Z"
  }
}
```

### Create Skill

```http
POST /workspaces/:workspaceId/skills
Authorization: Bearer <accessToken>
```

Request:

```json
{
  "name": "enterprise-discount-approval",
  "description": "Checks when enterprise discounts need finance sign-off.",
  "inputSchema": {},
  "outputSchema": {},
  "sourceDecisionIds": ["dec_123"]
}
```

Response `201`:

```json
{
  "data": {
    "id": "skl_456",
    "name": "enterprise-discount-approval",
    "version": "v1",
    "status": "draft"
  }
}
```

### Invoke Skill

```http
POST /workspaces/:workspaceId/skills/:skillName/invoke
Authorization: Bearer <accessToken or workspaceApiKey>
```

Request:

```json
{
  "input": {
    "customerTier": "premium",
    "daysSincePurchase": 42
  },
  "traceId": "trc_123"
}
```

Response `200`:

```json
{
  "data": {
    "skill": "premium-refund-policy",
    "version": "v4",
    "result": {
      "action": "approve_refund",
      "requiresApproval": false
    },
    "confidence": 96,
    "sources": [
      {
        "provider": "notion",
        "label": "Policy Library"
      }
    ]
  }
}
```

## Members API

### List Members

```http
GET /workspaces/:workspaceId/members
Authorization: Bearer <accessToken>
```

Response `200`:

```json
{
  "data": [
    {
      "id": "usr_123",
      "name": "Dana Reyes",
      "email": "dana@riverline.io",
      "role": "admin",
      "title": "Head of CX",
      "avatarColor": "#C2603A",
      "isCurrentUser": true
    }
  ],
  "meta": {
    "seatLimit": 12,
    "usedSeats": 4
  }
}
```

### Invite Member

```http
POST /workspaces/:workspaceId/members/invite
Authorization: Bearer <accessToken>
```

Request:

```json
{
  "email": "sam@riverline.io",
  "role": "viewer"
}
```

Response `201`:

```json
{
  "data": {
    "inviteId": "inv_123",
    "email": "sam@riverline.io",
    "role": "viewer",
    "status": "pending"
  }
}
```

Allowed member roles:

```txt
admin, editor, viewer
```

## Settings And API Keys

### Get Workspace Settings

```http
GET /workspaces/:workspaceId/settings
Authorization: Bearer <accessToken>
```

Response `200`:

```json
{
  "data": {
    "workspace": {
      "name": "Riverline",
      "domain": "riverline.io",
      "plan": "pro",
      "seatLimit": 12
    },
    "brainEndpoint": "https://riverline.Brainite.com/mcp"
  }
}
```

### Update Workspace Settings

```http
PATCH /workspaces/:workspaceId/settings
Authorization: Bearer <accessToken>
```

Request:

```json
{
  "name": "Riverline",
  "domain": "riverline.io"
}
```

Response `200`:

```json
{
  "data": {
    "workspace": {
      "id": "wrk_123",
      "name": "Riverline",
      "domain": "riverline.io"
    }
  }
}
```

### List Workspace API Keys

```http
GET /workspaces/:workspaceId/api-keys
Authorization: Bearer <accessToken>
```

Response `200`:

```json
{
  "data": [
    {
      "id": "key_123",
      "name": "Production MCP client",
      "prefix": "hph_live_abc1",
      "scopes": ["brain:query", "skills:invoke"],
      "createdAt": "2026-06-04T10:00:00.000Z",
      "lastUsedAt": "2026-06-04T10:22:31.000Z"
    }
  ]
}
```

### Create Workspace API Key

```http
POST /workspaces/:workspaceId/api-keys
Authorization: Bearer <accessToken>
```

Request:

```json
{
  "name": "Production MCP client",
  "scopes": ["brain:query", "skills:invoke"]
}
```

Response `201`:

```json
{
  "data": {
    "id": "key_123",
    "name": "Production MCP client",
    "apiKey": "hph_live_xxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
    "prefix": "hph_live_xxxx",
    "scopes": ["brain:query", "skills:invoke"],
    "createdAt": "2026-06-04T10:00:00.000Z"
  }
}
```

Allowed API key scopes:

```txt
brain:query, skills:invoke, sources:read, decisions:read
```

### Revoke Workspace API Key

```http
DELETE /workspaces/:workspaceId/api-keys/:keyId
Authorization: Bearer <accessToken>
```

Response `204`: no body.

## Usage API

### Get Usage Summary

```http
GET /workspaces/:workspaceId/usage?period=current_month
Authorization: Bearer <accessToken>
```

Response `200`:

```json
{
  "data": {
    "period": "current_month",
    "brainQueries": {
      "used": 18400,
      "limit": 27000,
      "spark": [12, 14, 13, 16, 18, 17, 18]
    },
    "mcpCalls": {
      "used": 42700,
      "spark": [30, 34, 38, 36, 40, 43, 43]
    },
    "skillsServed": {
      "count": 37,
      "spark": [28, 30, 32, 34, 35, 36, 37]
    }
  }
}
```

## MCP Endpoint

The UI exposes a workspace MCP endpoint:

```txt
https://riverline.Brainite.com/mcp
```

This should authenticate with a workspace API key and expose at least:

- `brain.query`
- `skills.list`
- `skills.invoke`
- `decisions.search`
- `sources.list`

Example MCP-style tool call payload:

```json
{
  "tool": "brain.query",
  "arguments": {
    "question": "How do enterprise discounts get approved?",
    "includeSources": true
  }
}
```

Example response:

```json
{
  "content": [
    {
      "type": "text",
      "text": "Discounts above 20% on annual contracts require VP Finance sign-off before the quote is sent."
    }
  ],
  "structuredContent": {
    "confidence": 78,
    "sources": [
      {
        "provider": "slack",
        "label": "#deal-desk"
      }
    ]
  }
}
```

## Rate Limits

Recommended defaults:

| Surface          | Limit                             |
| ---------------- | --------------------------------- |
| Auth endpoints   | 10 requests / minute / IP         |
| Dashboard API    | 300 requests / minute / user      |
| Brain query      | 60 requests / minute / workspace  |
| Skill invoke     | 600 requests / minute / workspace |
| OAuth callbacks  | 20 requests / minute / user       |
| API key creation | 5 requests / hour / admin         |

The MCP `query_brain` surface (port 8001) enforces the same two limits as the REST
path — the per-IP auth limit on `X-API-Key` resolution and the per-workspace brain
limit on the query itself — even though it has no HTTP route to decorate. A
throttled MCP call returns a `ToolError` whose message carries the retry hint.

Rate limit response:

```json
{
  "error": {
    "code": "rate_limited",
    "message": "Too many requests. Try again later."
  },
  "meta": {
    "retryAfterSeconds": 42
  }
}
```

## Webhooks And Jobs

Recommended internal jobs:

| Job               | Trigger                            | Result                              |
| ----------------- | ---------------------------------- | ----------------------------------- |
| `source.sync`     | Source connected or scheduled sync | Pull read-only content              |
| `brain.build`     | User finishes onboarding           | Extract decisions, policies, skills |
| `review.generate` | New uncertain extraction           | Create review item                  |
| `skill.generate`  | Approved decision or build step    | Create/update skill                 |
| `usage.rollup`    | Scheduled hourly                   | Aggregate usage metrics             |

For provider webhooks, validate provider signatures before enqueueing sync jobs.

```http
POST /webhooks/slack
POST /webhooks/github
POST /webhooks/jira
POST /webhooks/zendesk
```

Webhook response should be fast:

```json
{
  "data": {
    "received": true
  }
}
```

## Security Notes

- Source integrations must be read-only unless a future product decision explicitly enables write scopes.
- Encrypt provider refresh tokens at rest.
- Hash workspace API keys before storage.
- Scope every query by `workspaceId`.
- Do not let frontend clients send provider API secrets.
- Keep AI prompts in `app/integrations/prompts/`.
- Log request IDs, user IDs, workspace IDs, job IDs, and provider names. Do not log tokens, API keys, or raw provider secrets.
- Use repository/query helpers for complex database reads.
- Validate every request body with Pydantic v2 (`model_config = ConfigDict(extra="forbid")`).

## Frontend Implementation Notes

Current frontend state maps to these backend calls:

| UI Area            | Current Mock Data                                   | API Replacement                                                                                            |
| ------------------ | --------------------------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| Auth screen        | local state only                                    | `/auth/signup`, `/auth/signin`, OAuth endpoints                                                            |
| Onboarding company | local `data` state                                  | `/workspaces`, `/workspaces/:id/onboarding`                                                                |
| Source connect     | simulated timeouts                                  | `/workspaces/:id/sources/:provider/connect`, callback, `/workspaces/:id/sources`                           |
| Configure channels | `CHANNELS` fixture                                  | `/workspaces/:id/sources/:sourceId/channels`, `/workspaces/:id/sources/scope`                              |
| Build brain        | animation only                                      | `/workspaces/:id/brain/builds`, `/workspaces/:id/brain/builds/:buildId`                                    |
| First question     | static answer                                       | `/workspaces/:id/brain/query`                                                                              |
| Overview           | `DECISIONS`, `REVIEWS`, `SOURCE_HEALTH`, `ACTIVITY` | `/workspaces/:id/overview`, `/workspaces/:id/activity`                                                     |
| Decisions          | `DECISIONS` fixture                                 | `/workspaces/:id/decisions`, `/workspaces/:id/decisions/:decisionId`                                       |
| Reviews            | local queue                                         | `/workspaces/:id/reviews`, `/workspaces/:id/reviews/:reviewId/resolve`                                     |
| Sources            | `SOURCE_HEALTH` fixture                             | `/workspaces/:id/sources`                                                                                  |
| Skills             | `SKILLS` fixture                                    | `/workspaces/:id/skills`, `/workspaces/:id/skills/:skillId`                                                |
| Settings           | hardcoded Riverline data                            | `/workspaces/:id/settings`, `/workspaces/:id/members`, `/workspaces/:id/api-keys`, `/workspaces/:id/usage` |
