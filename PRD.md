# Company Brain — MVP Product Requirements Document

**Version:** 0.3  
**Status:** Draft — Phase 1 complete  
**Authors:** Founding team  
**Last updated:** May 11, 2026

---

## Table of Contents

1. [Overview](#1-overview)
2. [Problem Statement](#2-problem-statement)
3. [Goals and Non-Goals](#3-goals-and-non-goals)
4. [Users](#4-users)
5. [Architecture Overview](#5-architecture-overview)
6. [Tech Stack](#6-tech-stack)
7. [Data Model](#7-data-model)
8. [Knowledge Graph Ontology](#8-knowledge-graph-ontology)
9. [API Surface](#9-api-surface)
10. [Phases of Execution](#10-phases-of-execution)
11. [Open Questions](#11-open-questions)

---

## 1. Overview

Company Brain is an infrastructure layer that extracts a company's operational knowledge from every source it lives in — Slack, Zendesk, Notion, GitHub, Jira, email — structures it into versioned machine-readable executable skills, keeps it current as the company evolves, and exposes it to AI agents through a standard interface (MCP).

The output is not a document or a search result. It is an executable skill: a structured rule with conditions, actions, and dependencies that any MCP-compatible agent can query and act on with precision.

**The one-line pitch:** Every AI agent in the world will eventually hit the wall of not knowing company-specific logic. Company Brain is the infrastructure that solves that — permanently, and once.

---

## 2. Problem Statement

Companies deploying AI agents consistently hit the same failure mode: the agent handles generic tasks well but fails on company-specific edge cases — pricing exceptions, escalation rules, return policy thresholds, incident runbooks — because that logic exists nowhere a machine can read.

It lives in:

- People's heads (the CS lead who handles every exception by pattern recognition accumulated over years)
- Slack threads from 18 months ago (#ops-announcements, #pricing-approvals)
- Tens of thousands of Zendesk ticket resolutions that collectively encode how edge cases are actually handled
- Notion pages that describe how things worked before the last three policy changes
- GitHub pull requests and issue threads where engineering exceptions, rollback decisions, and operational fixes are discussed
- Jira tickets whose workflow history captures how incidents, escalations, and implementation requests are actually routed

AI agents have no authoritative source for this logic. They hallucinate policy, apply stale rules, or fail on edge cases in ways that damage customer trust and require expensive human correction.

Company Brain solves this. It mines operational logic from every source, structures it into executable skills backed by a temporal knowledge graph, keeps it current automatically, and serves it to agents via MCP with a single query.

---

## 3. Goals and Non-Goals

### MVP Goals

- Ingest real company data from five sources: Zendesk, Slack, Notion, GitHub, Jira
- Extract structured condition-action-dependency triples using the ExIde framework
- Discover actual behavioral patterns from Zendesk event logs using process mining (PM4Py)
- Build a temporal knowledge graph from extracted entities and relationships using Graphiti, with a custom Company Brain ontology (PolicyRule, CustomerTier, ExceptionCondition, ThresholdValue)
- Store versioned skills in a PostgreSQL registry backed by pgvector for semantic retrieval
- Serve skills to any MCP-compatible agent via FastMCP using hybrid retrieval: pgvector for semantic similarity, Graphiti graph traversal for relational precision
- Expose a REST API for non-MCP agent frameworks
- Detect high-signal source events and trigger skill re-extraction within 5 minutes (living currency)
- Surface low-confidence extractions for human review before publishing
- Demonstrate the full pipeline end-to-end with a Claude agent demo

### MVP Non-Goals

- Salesforce, HubSpot, Gmail, Gong, Confluence connectors (add post-MVP)
- Fine-tuned HuggingFace content classifier (Claude Haiku handles classification via prompt for MVP)
- Full grounding layer constraining agent actions to skill's explicit tool schemas (v2)
- Multi-tenant data isolation and PII redaction pipeline (required before external customers, not for internal prototype)
- n8n on Layer 5 (Claude agent calls FastMCP directly; living currency uses a plain FastAPI webhook endpoint)
- Persistent feedback loop processing episodic agent data back into the brain (stub only — log to table, process in v2)

---

## 4. Users

### Primary: AI engineers deploying support or ops agents

These are the people who keep hitting the "agent doesn't know our process" wall. They have a budget line for tooling, they can self-serve an API integration, and they do not need to go through procurement. At a 50–200 person B2B SaaS company, this is typically one person — the engineer who owns the agent stack.

**Pain they feel:** Writing and maintaining bespoke prompt context for every policy edge case. When policy changes, they have to find every prompt it touches and update each manually. When an edge case breaks the agent, they have to diagnose it manually.

**How they use Company Brain:** Point the MCP server at their agent framework. Query `query_brain(situation)` before each task. Receive matched skill with decision logic and tool schemas. Done.

### Secondary: CS or ops team leads who manage the human review queue

When the extraction engine produces a low-confidence skill update (70–90% confidence), it goes to the review queue instead of auto-publishing. This person reviews the proposed change, sees the source it came from, and approves or rejects. They are not technical.

**How they use Company Brain:** Simple review UI (FastAPI + minimal HTML for MVP). See proposed update, see source context, approve or reject.

---

## 5. Architecture Overview

Company Brain is organized into five layers. Each layer has a defined input, a specific responsibility, and a structured output that feeds the layer below. The knowledge graph and vector store together form the retrieval backbone.

```
┌──────────────────────────────────────────────────────────────────┐
│  LAYER 1 — DATA SOURCES                                          │
│  Airbyte OSS · Zendesk · Slack · Notion · GitHub · Jira          │
│  → Postgres staging (raw_content table)                          │
└────────────────────────────┬─────────────────────────────────────┘
                             │ raw documents
┌────────────────────────────▼─────────────────────────────────────┐
│  LAYER 2 — EXTRACTION ENGINE                                     │
│  Claude Haiku (classifier + ExIde) · PM4Py · OpenAI embeddings   │
│  → condition-action-dependency triples + embeddings              │
└────────────────────────────┬─────────────────────────────────────┘
                             │ structured triples
┌────────────────────────────▼─────────────────────────────────────┐
│  LAYER 3 — KNOWLEDGE CORE                                        │
│  ┌──────────────────────┐  ┌───────────────────┐  ┌──────────┐  │
│  │ Graphiti + Neo4j     │  │ Skills Registry   │  │  Redis   │  │
│  │ temporal graph       │  │ pgvector +        │  │  cache   │  │
│  │ custom ontology      │  │ PostgreSQL        │  │          │  │
│  └──────────────────────┘  └───────────────────┘  └──────────┘  │
└────────────────────────────┬─────────────────────────────────────┘
                             │ hybrid retrieval result
┌────────────────────────────▼─────────────────────────────────────┐
│  LAYER 4 — DELIVERY INTERFACE                                    │
│  FastMCP server (port 8001) · FastAPI REST (port 8000)           │
│  Hybrid retrieval: pgvector (semantic) + Graphiti (relational)   │
└────────────────────────────┬─────────────────────────────────────┘
                             │ grounded skill context
┌────────────────────────────▼─────────────────────────────────────┐
│  LAYER 5 — AGENT RUNTIME                                         │
│  Claude agent demo · FastMCP client · feedback log stub          │
└──────────────────────────────────────────────────────────────────┘
                             │ episodic feedback
                             └──────────────────► BRAIN UPDATES (v2)
```

### Living Currency (cross-cutting)

A webhook endpoint (`POST /ingest/event`) receives high-signal source events — Slack messages from designated channels, Notion page updates, GitHub issue or pull request updates, and Jira issue transitions or comments. On receipt, the extraction engine re-processes only the affected skill and updates both the pgvector skills registry and the Graphiti graph. If confidence ≥ 90%, the skill auto-publishes with a version bump. If 70–89%, it goes to the review queue. Below 70%, it is logged and discarded. Target latency from source event to published update: under 5 minutes.

### Hybrid Retrieval

For every agent query, retrieval runs in two stages:

1. **Semantic stage (pgvector):** cosine similarity search over skill description embeddings returns candidate skill IDs ranked by semantic closeness
2. **Graph stage (Graphiti):** for the top candidates, Graphiti graph traversal resolves relational context — which rules override others, which exception conditions apply, which customer tier gates a specific action. Graph hits surface above pure semantic matches.

The combined result is the highest-precision skill match Company Brain can produce.

---

## 6. Tech Stack

| Layer | Component                | Technology                         | Notes                                                                               |
| ----- | ------------------------ | ---------------------------------- | ----------------------------------------------------------------------------------- |
| 1     | Data connectors          | Airbyte OSS                        | Self-hosted via `abctl local install`. Runs separately from main compose.           |
| 1     | Ingestion destination    | Postgres 16                        | `raw_content` table. Airbyte writes here.                                           |
| 2     | Content classifier       | Claude Haiku                       | Prompt-based: rule / fact / event / noise                                           |
| 2     | Rule extractor           | Claude Haiku + ExIde               | Two-stage: pseudo-code intermediate → condition-action-dependency triple            |
| 2     | Process mining           | PM4Py                              | Runs on Zendesk ticket event logs. Discovers actual decision patterns.              |
| 2     | Embeddings               | OpenAI text-embedding-3-small      | 1536 dimensions. Used for pgvector retrieval and Graphiti entity embeddings.        |
| 3     | Temporal knowledge graph | Graphiti (Apache 2.0) + Neo4j 5.26 | Custom Company Brain ontology. Hybrid retrieval: semantic + BM25 + graph traversal. |
| 3     | Vector store             | pgvector (Postgres extension)      | IVFFlat index on skill description embeddings. Semantic retrieval layer.            |
| 3     | Skills registry          | PostgreSQL 16                      | Versioned skill records, conflict tracking, audit log.                              |
| 3     | Hot-path cache           | Redis 7                            | 5-minute TTL on skill search results. Invalidated on publish/update.                |
| 4     | MCP server               | FastMCP (Python)                   | Exposes `query_brain` tool. SSE transport on port 8001.                             |
| 4     | REST API                 | FastAPI                            | CRUD + search + webhook + review endpoints on port 8000.                            |
| 5     | Agent demo               | Anthropic Python SDK               | Claude calls FastMCP server directly. No orchestration framework dependency.        |
| Infra | Container runtime        | Docker + Docker Compose            | All services. Airbyte has its own compose managed separately.                       |

### Graphiti configuration note

Graphiti defaults to OpenAI for its internal LLM calls (entity extraction, deduplication, relationship classification). Since we are already using the OpenAI API for embeddings, Graphiti runs on the same key with no additional provider setup. Install: `pip install graphiti-core`. Entity extraction concurrency is controlled via `SEMAPHORE_LIMIT` — set to 5 for MVP to avoid rate limit errors during bulk ingestion.

---

## 7. Data Model

### `raw_content` — Airbyte staging

```sql
CREATE TABLE raw_content (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  source         VARCHAR(50),        -- 'zendesk' | 'slack' | 'notion' | 'github' | 'jira'
  source_id      VARCHAR(255),       -- original record ID from source system
  content        TEXT,
  metadata       JSONB,
  content_type   VARCHAR(20),        -- 'rule' | 'fact' | 'event' | 'noise'
  graph_ingested BOOLEAN DEFAULT FALSE,
  ingested_at    TIMESTAMP DEFAULT NOW()
);
```

### `skills` — versioned skills registry

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE skills (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name           VARCHAR(255) UNIQUE NOT NULL,
  version        INTEGER DEFAULT 1,
  confidence     FLOAT,
  description    TEXT,             -- semantic retrieval field — when-to-invoke framing
  decision_logic TEXT,             -- IF/THEN rule body in SKILL.md format
  tool_schemas   JSONB,            -- [{name, params, returns}]
  source_ids     JSONB DEFAULT '[]',
  conflict_flags JSONB DEFAULT '[]',
  graph_node_ids JSONB DEFAULT '[]',  -- Graphiti node UUIDs for this skill's entities
  status         VARCHAR(20) DEFAULT 'draft',  -- draft | pending_review | published | archived
  embedding      VECTOR(1536),
  created_at     TIMESTAMP DEFAULT NOW(),
  updated_at     TIMESTAMP DEFAULT NOW()
);

CREATE INDEX ON skills USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
```

### `skill_versions` — full change history

```sql
CREATE TABLE skill_versions (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  skill_id       UUID REFERENCES skills(id),
  version        INTEGER,
  decision_logic TEXT,
  confidence     FLOAT,
  changed_by     VARCHAR(50),      -- 'system' | reviewer user ID
  created_at     TIMESTAMP DEFAULT NOW()
);
```

### `review_queue` — human-in-the-loop gates

```sql
CREATE TABLE review_queue (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  skill_id        UUID REFERENCES skills(id),
  proposed_update JSONB,
  confidence      FLOAT,
  reason          TEXT,
  status          VARCHAR(20) DEFAULT 'pending',  -- pending | approved | rejected
  created_at      TIMESTAMP DEFAULT NOW(),
  resolved_at     TIMESTAMP
);
```

### `agent_interactions` — episodic feedback stub

```sql
CREATE TABLE agent_interactions (
  id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  skill_id           UUID REFERENCES skills(id),
  query              TEXT,
  matched_confidence FLOAT,
  graph_path         JSONB,         -- edges traversed during Graphiti retrieval
  agent_action       JSONB,
  human_override     BOOLEAN DEFAULT FALSE,
  created_at         TIMESTAMP DEFAULT NOW()
);
```

---

## 8. Knowledge Graph Ontology

Company Brain uses Graphiti with a prescribed domain ontology. Custom entity types and edge types are defined as Pydantic models and passed to Graphiti at ingestion time. Graphiti handles entity deduplication, temporal validity windows, and provenance tracking automatically.

### Entity types

> **Note:** graphiti-core 0.7+ uses plain `pydantic.BaseModel` for custom entity and edge types. The `EntityNode` / `EntityEdge` base classes from earlier versions no longer exist.

```python
from pydantic import BaseModel, Field

class PolicyRule(BaseModel):
    """A discrete operational rule that governs an agent decision."""
    condition: str = Field(description="The condition under which this rule applies")
    action: str = Field(description="The action to execute when condition is met")
    confidence: float = Field(description="Extraction confidence 0.0-1.0")
    source_type: str = Field(description="zendesk | slack | notion | github | jira | process_mining")

class CustomerTier(BaseModel):
    """A customer classification that gates specific rules or actions."""
    tier_name: str = Field(description="Name of the tier e.g. standard, enterprise, B2B")
    crm_field: str = Field(description="CRM field that stores this value", default="")

class ThresholdValue(BaseModel):
    """A numeric threshold that determines rule branching."""
    value: float = Field(description="The threshold number")
    unit: str = Field(description="Unit e.g. days, USD, percentage")
    context: str = Field(description="What this threshold applies to")

class ExceptionCondition(BaseModel):
    """A condition that overrides or modifies a base rule."""
    override_type: str = Field(description="time_window | policy | routing")
    trigger: str = Field(description="What triggers this exception")
```

### Edge types

```python
from pydantic import BaseModel, Field

class OverridesEdge(BaseModel):
    """Source rule overrides target rule when its condition is met."""
    priority: int = Field(description="Override priority — higher wins on conflict", default=1)

class HasExceptionForEdge(BaseModel):
    """Source policy has a defined exception path for target entity."""
    exception_action: str = Field(description="Action taken in the exception case")

class GovernsEdge(BaseModel):
    """Source rule governs the handling of target entity type."""
    scope: str = Field(description="Scope of governance e.g. time, amount, routing")

class RequiresRoutingToEdge(BaseModel):
    """Source entity type requires routing to target role or system."""
    routing_condition: str = Field(description="Condition that triggers routing")
```

### Example graph subgraph (return policy domain)

```
(DamagedInTransit: ExceptionCondition) -[OVERRIDES]->       (TimeWindowRule: PolicyRule)
(TimeWindowRule: PolicyRule)           -[GOVERNS]->          (ReturnRequest: PolicyRule)
(RefundPolicy: PolicyRule)             -[HAS_EXCEPTION_FOR]-> (PremiumAccount: CustomerTier)
(B2BAccount: CustomerTier)             -[REQUIRES_ROUTING_TO]-> (AccountManager: PolicyRule)
(ThirtyDayWindow: ThresholdValue)      -[GOVERNS]->          (ReturnRequest: PolicyRule)
```

### Graphiti ingestion pattern

```python
from graphiti_core import Graphiti
from datetime import datetime, timezone

ENTITY_TYPES = {
    "PolicyRule": PolicyRule,
    "CustomerTier": CustomerTier,
    "ThresholdValue": ThresholdValue,
    "ExceptionCondition": ExceptionCondition,
}

async def ingest_to_graph(skill_name: str, content: str, source_id: str):
    graphiti = await get_graphiti()
    episode = await graphiti.add_episode(
        name=skill_name,
        episode_body=content,
        source_description=f"Extracted from {source_id}",
        reference_time=datetime.now(timezone.utc),
        entity_types=ENTITY_TYPES,
    )
    return episode
```

---

## 9. API Surface

### FastAPI REST — port 8000

| Method | Path                                 | Description                                                          |
| ------ | ------------------------------------ | -------------------------------------------------------------------- |
| GET    | `/health`                            | Returns `{"status": "ok", "db": bool, "redis": bool, "neo4j": bool}` |
| GET    | `/skills/search?q={query}&limit={k}` | Hybrid retrieval: pgvector + Graphiti graph traversal                |
| GET    | `/skills/{skill_id}`                 | Return full skill body                                               |
| GET    | `/skills/{skill_id}/versions`        | Return full version history                                          |
| POST   | `/skills`                            | Create skill (draft status)                                          |
| PATCH  | `/skills/{skill_id}`                 | Update skill fields                                                  |
| POST   | `/ingest/event`                      | Living currency webhook — Slack, Notion, GitHub, or Jira events      |
| POST   | `/ingest/batch`                      | Trigger manual extraction run over unprocessed `raw_content`         |
| GET    | `/review`                            | List pending review queue items                                      |
| POST   | `/review/{item_id}/approve`          | Approve and publish proposed skill update                            |
| POST   | `/review/{item_id}/reject`           | Reject proposed update                                               |

### FastMCP server — port 8001

```python
@mcp.tool()
async def query_brain(situation: str, entities: dict = {}) -> dict:
    """
    Query the company brain for the operational skill that matches this situation.
    Call this before executing any company-specific task.
    Returns decision logic, tool schemas, confidence score, and graph_context
    showing resolved overrides and dependencies.
    """
```

---

## 10. Phases of Execution

---

### Phase 1 — App Architecture ✅ Complete

**Scope:** No business logic. Establish the complete project structure, Docker environment, database schema, Neo4j instance, and empty service stubs so that every subsequent phase slots cleanly into a known location with zero structural rework.

**Deliverables:**

#### Folder structure

```
company-brain/
├── docker-compose.yml
├── .env.example
├── schema.sql
├── brain-api/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── main.py                    # FastAPI app — registers routers, lifespan events
│   ├── config.py                  # Settings from env via pydantic-settings
│   ├── database.py                # Async Postgres pool (asyncpg)
│   ├── cache.py                   # Redis client (redis-py async)
│   ├── graph.py                   # Graphiti client init + ontology registration
│   ├── routers/
│   │   ├── __init__.py
│   │   ├── health.py              # GET /health — checks Postgres, Redis, Neo4j
│   │   ├── skills.py              # Skill CRUD + search (stubs — return 501)
│   │   ├── ingest.py              # POST /ingest/event + /ingest/batch (stubs)
│   │   └── review.py              # Review queue endpoints (stubs)
│   ├── services/
│   │   ├── __init__.py
│   │   ├── classifier.py          # Content type classifier — stub
│   │   ├── extractor.py           # ExIde pipeline — stub
│   │   ├── embedder.py            # OpenAI embedding wrapper — stub
│   │   ├── process_miner.py       # PM4Py wrapper — stub
│   │   ├── graph_builder.py       # Graphiti ingestion + ontology types — stub
│   │   └── skill_writer.py        # Skill registry + graph write logic — stub
│   ├── mcp_server/
│   │   ├── __init__.py
│   │   └── server.py              # FastMCP server with query_brain stub
│   └── models/
│       ├── __init__.py
│       └── schemas.py             # Pydantic request/response models
├── airbyte/
│   └── README.md                  # Connector setup: Zendesk, Slack, Notion, GitHub, Jira
└── agent-demo/
    ├── demo.py                    # Claude agent test script — stub
    └── requirements.txt
```

#### `docker-compose.yml`

```yaml
services:
  postgres:
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_DB: company_brain
      POSTGRES_USER: cb
      POSTGRES_PASSWORD: cb_secret
    volumes:
      - pgdata:/var/lib/postgresql/data
      - ./schema.sql:/docker-entrypoint-initdb.d/01-schema.sql
    ports:
      - "5432:5432"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U cb -d company_brain"]
      interval: 5s
      timeout: 5s
      retries: 5

  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 5

  neo4j:
    image: neo4j:5.26-community
    environment:
      NEO4J_AUTH: neo4j/neo4j_secret
      NEO4J_dbms_memory_heap_initial__size: 512m
      NEO4J_dbms_memory_heap_max__size: 1G
      NEO4J_PLUGINS: '["apoc"]'
    volumes:
      - neo4jdata:/data
    ports:
      - "7474:7474" # Neo4j Browser
      - "7687:7687" # Bolt protocol (Graphiti connects here)
    healthcheck:
      test: ["CMD-SHELL", "wget -qO- http://localhost:7474 || exit 1"]
      interval: 10s
      timeout: 10s
      retries: 10

  brain-api:
    build: ./brain-api
    environment:
      DATABASE_URL: postgresql://cb:cb_secret@postgres/company_brain
      REDIS_URL: redis://redis:6379
      NEO4J_URI: bolt://neo4j:7687
      NEO4J_USER: neo4j
      NEO4J_PASSWORD: neo4j_secret
      ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY}
      OPENAI_API_KEY: ${OPENAI_API_KEY}
      SEMAPHORE_LIMIT: 5
      MCP_PORT: 8001
      API_PORT: 8000
    ports:
      - "8000:8000"
      - "8001:8001"
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
      neo4j:
        condition: service_healthy
    volumes:
      - ./brain-api:/app
    command: python main.py

volumes:
  pgdata:
  neo4jdata:
```

#### `brain-api/requirements.txt`

> **Note:** Using `>=` floor pins rather than exact pins. `fastmcp==0.4.0` and `graphiti-core==0.3.0` from the original spec are outdated — both packages have breaking API changes in their current major versions.

```
fastapi>=0.115.0
uvicorn[standard]>=0.30.0
asyncpg>=0.29.0
redis[asyncio]>=5.0.0
pgvector>=0.3.0
pydantic>=2.7.0
pydantic-settings>=2.3.0
fastmcp>=2.0.0
anthropic>=0.34.0
openai>=1.40.0
graphiti-core>=0.7.0
neo4j>=5.26.0
pm4py>=2.7.11
pandas>=2.2.0
httpx>=0.27.0
python-dotenv>=1.0.0
jinja2>=3.1.4
```

#### `brain-api/config.py`

```python
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    database_url: str
    redis_url: str
    neo4j_uri: str
    neo4j_user: str
    neo4j_password: str
    anthropic_api_key: str
    openai_api_key: str
    semaphore_limit: int = 5
    mcp_port: int = 8001
    api_port: int = 8000

    class Config:
        env_file = ".env"

settings = Settings()
```

#### `brain-api/graph.py`

```python
from graphiti_core import Graphiti
from config import settings

_graphiti: Graphiti | None = None

async def init_graphiti():
    global _graphiti
    _graphiti = Graphiti(
        uri=settings.neo4j_uri,
        user=settings.neo4j_user,
        password=settings.neo4j_password,
    )
    await _graphiti.build_indices_and_constraints()

async def get_graphiti() -> Graphiti:
    if _graphiti is None:
        raise RuntimeError("Graphiti not initialized")
    return _graphiti

async def check_neo4j_health() -> bool:
    """Defensive connectivity check — handles varying graphiti driver internals."""
    if _graphiti is None:
        return False
    try:
        driver = _graphiti.driver
        if hasattr(driver, "execute_query"):
            await driver.execute_query("RETURN 1 AS n")
        elif hasattr(driver, "_driver"):
            await driver._driver.verify_connectivity()
        return True
    except Exception:
        return False

async def close_graphiti():
    if _graphiti:
        await _graphiti.close()
```

#### `brain-api/main.py`

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from routers import health, skills, ingest, review
from database import init_db_pool, close_db_pool
from cache import init_redis, close_redis
from graph import init_graphiti, close_graphiti
import asyncio
from mcp_server.server import run_mcp_server

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db_pool()
    await init_redis()
    await init_graphiti()
    asyncio.create_task(run_mcp_server())
    yield
    await close_graphiti()
    await close_db_pool()
    await close_redis()

app = FastAPI(title="Company Brain API", version="0.2.0", lifespan=lifespan)

app.include_router(health.router)
app.include_router(skills.router, prefix="/skills")
app.include_router(ingest.router, prefix="/ingest")
app.include_router(review.router, prefix="/review")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
```

#### `brain-api/routers/health.py`

```python
from fastapi import APIRouter
from database import get_pool
from cache import get_redis
from graph import check_neo4j_health

router = APIRouter()

@router.get("/health")
async def health():
    status = {"status": "ok", "db": False, "redis": False, "neo4j": False}
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        status["db"] = True
    except Exception:
        pass
    try:
        r = await get_redis()
        await r.ping()
        status["redis"] = True
    except Exception:
        pass
    status["neo4j"] = await check_neo4j_health()
    return status
```

#### `brain-api/routers/skills.py` (stub pattern — all other routers follow this)

```python
from fastapi import APIRouter, HTTPException

router = APIRouter()

@router.get("/search")
async def search_skills(q: str, limit: int = 5):
    raise HTTPException(status_code=501, detail="Not implemented — Phase 4")

@router.get("/{skill_id}")
async def get_skill(skill_id: str):
    raise HTTPException(status_code=501, detail="Not implemented — Phase 4")

@router.get("/{skill_id}/versions")
async def get_versions(skill_id: str):
    raise HTTPException(status_code=501, detail="Not implemented — Phase 4")
```

#### `brain-api/mcp_server/server.py`

> **Note:** `mcp.run_async(...)` was renamed to `mcp.run_http_async(...)` in FastMCP 2.x.

```python
from fastmcp import FastMCP

mcp = FastMCP("Company Brain")

@mcp.tool()
async def query_brain(situation: str, entities: dict = {}) -> dict:
    """
    Query the company brain for the operational skill that matches this situation.
    Call this before executing any company-specific task.
    Returns decision logic, tool schemas, confidence score, and graph_context
    showing resolved overrides and dependencies.
    """
    return {
        "status": "stub",
        "message": "MCP server running. Skill retrieval implemented in Phase 5.",
        "situation_received": situation,
    }

async def run_mcp_server():
    await mcp.run_http_async(transport="sse", host="0.0.0.0", port=8001)
```

#### `.env.example`

```
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
DATABASE_URL=postgresql://cb:cb_secret@localhost/company_brain
REDIS_URL=redis://localhost:6379
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=neo4j_secret
SEMAPHORE_LIMIT=5
MCP_PORT=8001
API_PORT=8000
```

#### Phase 1 acceptance criteria

- `docker compose up` starts all four services with no errors
- `GET localhost:8000/health` returns `{"status": "ok", "db": true, "redis": true, "neo4j": true}`
- `GET localhost:8000/skills/search?q=test` returns HTTP 501
- FastMCP server reachable on port 8001
- All tables exist in Postgres (`\dt` in psql)
- pgvector extension installed (`SELECT * FROM pg_extension WHERE extname = 'vector'`)
- Neo4j Browser accessible at `localhost:7474`
- Graphiti indices and constraints built (`SHOW INDEXES` in Neo4j Browser returns Graphiti-managed indexes)

---

### Phase 2 — Layer 1: Data Ingestion

**Scope:** Airbyte setup and data flow into `raw_content`.

**Deliverables:**

- Airbyte installed locally via `abctl local install`
- Five source connectors configured: Zendesk (tickets + comments), Slack (target channels), Notion (pages + databases), GitHub (issues + pull requests), Jira (issues + comments + transitions)
- Destination connector: Postgres, writing to `raw_content` with correct `source` field populated
- Full historical sync completed for all five sources
- Incremental hourly sync running
- `airbyte/README.md` with step-by-step connector configuration

**Acceptance criteria:**

- `SELECT COUNT(*), source FROM raw_content GROUP BY source` shows rows from all five sources
- Spot check 5 rows from each source — `content` and `metadata` populated correctly
- Incremental sync runs without errors on second trigger

---

### Phase 3 — Layer 2: Extraction Engine

**Scope:** Content classification, ExIde rule extraction, PM4Py process mining, embedding generation, skill writing.

**Deliverables:**

- `classifier.py` — calls Claude Haiku to label each unprocessed `raw_content` row as rule / fact / event / noise
- `extractor.py` — two-stage ExIde pipeline: raw text → pseudo-code intermediate → condition-action-dependency JSON triple
- `process_miner.py` — reads Zendesk ticket event rows as PM4Py event log, discovers dominant resolution patterns, outputs rule candidates
- `embedder.py` — calls OpenAI `text-embedding-3-small` on skill description field
- `skill_writer.py` — takes extracted triples, computes confidence, writes to `skills` table with status routing: ≥90% → published, 70–89% → pending_review, <70% → draft
- `POST /ingest/batch` — triggers full extraction run over unprocessed `raw_content` rows

**Acceptance criteria:**

- `POST /ingest/batch` produces skill rows in `skills` table
- At least 3 published skills from a Zendesk data set
- At least 1 conflict-flagged skill in `review_queue`
- Skills with confidence < 70% remain as draft
- Each published skill has a non-null `embedding` vector

---

### Phase 4 — Layer 3: Knowledge Core

**Scope:** Graphiti graph ingestion with custom ontology, pgvector search, Redis caching, skill versioning, human review workflow.

**Deliverables:**

- `graph_builder.py` — full implementation with custom entity types (PolicyRule, CustomerTier, ThresholdValue, ExceptionCondition) and edge types (OverridesEdge, HasExceptionForEdge, GovernsEdge, RequiresRoutingToEdge). Ingests each extracted skill into Graphiti as an episode. Stores returned Graphiti node UUIDs in `skills.graph_node_ids`. Sets `raw_content.graph_ingested = true` on completion.
- pgvector cosine similarity search over published skills
- Redis cache on search results (5-minute TTL, invalidated on publish/update)
- Version bump logic: every update to a published skill creates a `skill_versions` row and increments `skills.version`
- All skill and review endpoints live
- Minimal review UI: FastAPI Jinja2 template listing pending items with source context and approve/reject buttons

**Acceptance criteria:**

- `GET localhost:8000/skills/search?q=refund+request` returns ranked skills with scores
- Second identical query served from Redis
- Neo4j Browser shows extracted entity nodes with custom type labels and typed edges
- Graphiti search returns related entities when queried with a policy situation string
- Approving a review item increments version, changes status to published, invalidates cache
- Rejected item does not appear in published skills

---

### Phase 5 — Layer 4: Delivery Interface

**Scope:** FastMCP server wired to hybrid retrieval. REST endpoints complete. Living currency webhook active.

**Deliverables:**

- `query_brain` MCP tool fully wired: pgvector semantic search → Graphiti graph traversal → merged result → Redis cached → interaction logged
- Skill response schema includes `graph_context` field with resolved overrides and dependencies
- `POST /ingest/event` active — receives Slack, Notion, GitHub, or Jira webhook, classifies, runs ExIde, ingests to graph, routes by confidence

**Acceptance criteria:**

- Calling `query_brain(situation=...)` returns a published skill with populated `graph_context`
- A mock Slack policy announcement or Jira workflow update posted to `POST /ingest/event` produces an updated skill within 5 minutes
- Graph traversal edges logged in `agent_interactions.graph_path`

---

### Phase 6 — Layer 5: Agent Demo

**Scope:** Working Claude agent resolving a customer support scenario end-to-end. Recorded as a demo.

**Demo scenario:**

1. Input: "Customer requesting refund, 38 days post-purchase, $200 order, claims item arrived damaged in transit"
2. Agent calls `query_brain(situation=...)`
3. Brain returns skill with `graph_context` showing DamagedInTransit OVERRIDES TimeWindowRule
4. Agent applies override: approve_return + initiate_carrier_claim despite the time window
5. Agent prints resolution grounded in the returned skill
6. Interaction logged to `agent_interactions`

**Acceptance criteria:**

- Demo runs end-to-end in under 2 minutes
- Agent produces correct resolution traceable to a published skill and graph-resolved override
- Zero hallucinated policy in agent output

---

## 11. Open Questions

| Question                                                                                                                                                             | Priority                                                  |
| -------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------- |
| Which data set to use for Phase 2 — a pilot customer's Zendesk/Slack/Notion/GitHub/Jira, or a synthetic data set built to stress-test the extraction pipeline?      | High — needed before Phase 2                              |
| Graphiti GitHub issue #567: custom entity type labels not always persisting correctly to Neo4j. Apply workaround (re-ingest with types) or pin to a patched version? | High — needed before Phase 4                              |
| Graphiti structured output note: works best with OpenAI. Confirm OpenAI is the entity extraction LLM throughout the pipeline.                                        | High                                                      |
| Review queue notification — email, Slack DM, or polling the UI?                                                                                                      | Medium — needed for Phase 4                               |
| Pricing model for first customers — platform fee + per-query, flat monthly, or outcome-based?                                                                        | Medium — needed before any external customer conversation |
| FalkorDB as Neo4j alternative — lighter Docker footprint, sub-10ms queries, Graphiti-compatible. Evaluate if Neo4j memory is a concern on dev machines.              | Low                                                       |
