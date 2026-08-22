# Company Brain — Product Specification

**Version:** 1.4  
**Status:** Active  
**Last updated:** August 2026

> **What's new in 1.4:** successful **agent runs** become an extraction source. A clean
> trace (tool calls, file exploration, scripts, outcome) is distilled through the existing
> pipeline into a reviewed, versioned **procedure skill** — closing a self-improving loop
> (more agent usage → cheaper, more accurate agents). See Section 3 (Moat #4), Section 6
> (Self-Improving Loop), Features 29–34, Process 8, and Phase 7.

---

## Table of Contents

1. [Overview](#1-overview)
2. [Problem Statement](#2-problem-statement)
3. [Positioning & Competitive Landscape](#3-positioning--competitive-landscape)
4. [What We Are Not Building](#4-what-we-are-not-building)
5. [Users](#5-users)
6. [Architecture](#6-architecture)
7. [Tech Stack](#7-tech-stack)
8. [Skill Format](#8-skill-format)
9. [Data Model](#9-data-model)
10. [Source Authority Config](#10-source-authority-config)
11. [Features](#11-features)
12. [Processes](#12-processes)
13. [Onboarding Flow](#13-onboarding-flow)
14. [API Surface](#14-api-surface)
15. [Success Metrics](#15-success-metrics)
16. [Phases of Execution](#16-phases-of-execution)
17. [Open Questions](#17-open-questions)

---

## 1. Overview

Company Brain is the missing layer between raw company data and reliable AI automation.

Every company runs on operational knowledge that exists nowhere a machine can read — in Slack threads, Notion pages, Google Drive documents, Zendesk ticket resolutions, GitHub pull request discussions, and people's heads. AI agents fail on company-specific tasks not because the models are weak but because this knowledge is inaccessible to them.

Company Brain solves this by connecting to every source that knowledge lives in, extracting it into structured executable skills, keeping those skills current as the company evolves, and serving them to any AI agent through a standard interface.

The output is not a search result or a document summary. It is an executable skill: a structured, versioned, human-reviewed description of how the company handles a specific situation, complete with trigger conditions, decision logic, exceptions, and the actions an agent can take.

**One-line pitch:** Every AI agent in the world will eventually hit the wall of not knowing company-specific logic. Company Brain is the infrastructure that solves that — once, permanently, for every company.

---

## 2. Problem Statement

Companies deploying AI agents hit the same failure mode consistently: the agent handles generic tasks well but collapses on company-specific edge cases. Pricing exceptions, escalation rules, return policy thresholds, incident runbooks — this logic exists nowhere a machine can reliably read.

It lives in:

- Slack threads from 14 months ago where a policy decision was made in message 47 of a 50-message chain
- Notion pages that describe how things worked before the last three policy revisions
- Google Drive documents — the SOPs, policy decks, and process runbooks shared across the company but never wired into any system an agent can read
- Zendesk ticket resolutions that collectively encode how edge cases are actually handled, but are buried in thousands of tickets
- GitHub pull request review comments where engineering exceptions and rollback decisions were debated
- Jira tickets whose comment history captures how incidents are actually routed
- The head of the CS lead who handles every exception by pattern recognition built over years

AI agents have no authoritative source for any of this. They hallucinate policy, apply stale rules, or fail on edge cases in ways that damage customer trust and require expensive human correction.

The right fix is not better prompting or larger context windows. It is a dedicated infrastructure layer that extracts this knowledge, structures it, keeps it current, and serves it to agents on demand. That is Company Brain.

---

## 3. Positioning & Competitive Landscape

### Category validation

This product is a direct answer to Y Combinator's official Request for Startups. Tom Blomfield's **"Company Brain"** RFS asks for "a system that pulls knowledge out of all these fragmented sources, structures it, keeps it current, and turns it into an executable skills file for AI... a living map of how a company works: how refunds get handled, how pricing exceptions are decided." Diana Hu's **"AI Operating System for Companies"** RFS asks for the closed feedback loop the same system enables. The problem is validated; the category is forming now, which means speed matters.

### Competitive landscape (as of July 2026)

| Competitor | What they do | How we differ |
| --- | --- | --- |
| **Hyperspell** (YC F25, ~$2M raised) | Memory layer API for agents; connectors for Slack, Gmail, Notion, Drive. Tagline: "Your Company Brain." | Sells *recall*. We sell *decision logic*: structured skills with base rules, exceptions, and actions. |
| **Hyper** (YC P26) | Knowledge graph of timestamped subject-predicate-object facts with provenance and "supersedes" relations. | Closest in ambition, but auto-extracts facts with no human gate. Public criticism centers on fact hallucination, silent staleness, and no auditability — exactly what our review queue and contradiction detection solve. |
| **Cerenovus** (YC W26) | Aggregates company files into a markdown knowledge graph; infers operational inefficiencies for executives. | Different buyer (executives, not agent engineers) and different output (analysis, not executable skills). |
| **GBrain** (open source) | Typed knowledge graph, zero LLM calls per write, large OSS adoption. | Free floor for small tech-forward teams. We do not compete for that segment. |
| **Mem0 / Zep / Letta** (Mem0: $24M Series A) | Agent memory infrastructure: vectors, temporal graphs, memory-OS runtimes. | They store what *an agent* experienced — raw episodic recall, an increasingly commodity layer. We ingest the same raw material (agent run traces) but **distil it into human-reviewed, versioned procedures** the whole company's agents inherit. Storing episodes is not a moat; a reviewed procedure corpus is. Complementary — an agent can use both. |
| **Glean / Dust / Onyx** | Enterprise search and chat-over-documents. | Solved problem, different product. We return executable skills, not search results (see Section 4). |

### Differentiation thesis

Every funded entrant in this category is retrieval-first: they make company data *findable*. None of them ship what agents actually need to act safely: **human-reviewed, versioned, executable decision logic with contradiction detection and full audit trails**. Industry criticism of the retrieval-first approach converges on three failures — non-determinism, no governance, no auditability. Our review queue, source-authority tiers, confidence routing, contradiction cards, and `skill_versions` history are a direct answer to all three. The exceptions-table design (a new policy appends a row to an existing skill rather than creating a new document) is a structural insight no competitor has.

### Moat

1. **The reviewed skill corpus compounds.** Every human approval makes the brain more trustworthy and harder to replicate. Retrieval indexes can be rebuilt overnight; a corpus of human-verified operational logic cannot.
2. **Portability as a wedge against lock-in fear.** Skills are plain versioned markdown, exportable at any time via `GET /skills/export`. The loudest criticism of competitors is vendor lock-in on accumulated organizational intelligence. "Your brain is yours — export it anytime" is a differentiator we get for free and must never break.
3. **The review workflow is the trust layer.** Regulated and risk-sensitive operations (the acknowledged gap in every competitor) require exactly the determinism and traceability our pipeline produces by construction.
4. **Usage compounds into the corpus (the self-improving loop).** Every *successful* agent run is itself an extraction source: its trace is distilled into a reviewed procedure skill, so the next agent that hits the same task gets the shortest known path instead of re-deriving it. More agent usage → more reviewed procedures → cheaper and more accurate agents → more usage. This is the flywheel a retrieval index cannot bootstrap, because the raw material (traces of *this company's* agents succeeding at *this company's* tasks) only exists where the delivery layer already sits. Critically, what compounds is **reviewed procedures**, not raw agent memory — episodic memory stores are becoming a commodity (Mem0, Zep, Letta); a human-approved procedure corpus with version history and provenance is not.

### Launch wedge

We launch narrow: **the policy brain for AI customer-support agents at 50–500 person B2B SaaS companies running Zendesk**. The demo scenario (refund handling), the Zendesk connector, and the CS-lead reviewer persona all already point here. Engineering runbooks, incident routing, and ops automation are expansion surfaces, not launch surfaces.

---

## 4. What We Are Not Building

These decisions reflect deliberate choices to keep the MVP shippable and the product honest.

| Not building                                | Reason                                                                                                                                                          |
| ------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| A chatbot over documents                    | That is a solved problem. Company Brain produces executable skills, not search results.                                                                         |
| A knowledge graph (Neo4j + Graphiti)        | Boundary classification and override detection via vector similarity + LLM calls handles what the graph was supposed to do, without the operational complexity. |
| Airbyte batch ingestion                     | Replaced entirely by MCP connectors + webhooks. Sources are queried directly.                                                                                   |
| PM4Py process mining                        | A Sonnet call over resolved Zendesk tickets produces equivalent output for MVP purposes.                                                                        |
| ExIde two-stage extraction                  | Replaced by a cleaner two-pass extraction design with better separation of concerns.                                                                            |
| Multi-tenant PII redaction                  | Required before external enterprise customers. Not for internal prototype.                                                                                      |
| Fine-tuned content classifier               | The Groq fast classifier (`llama-3.3-70b-versatile`) via prompt handles classification at MVP scale.                                                            |
| Salesforce, HubSpot, Gong connectors        | Post-MVP. Six sources are sufficient to prove the extraction pipeline.                                                                                         |
| Raw agent memory / episodic recall store    | We ingest agent run traces (Features 29–34) but never serve them back verbatim. Storing "what the agent did last Tuesday" is the commodity memory-layer game (Mem0, Zep, Letta). We keep the *distilled, human-approved procedure* and expire the raw trace. Customers who want episodic recall pair us with a memory layer. |

> **Note on Gmail:** a Gmail connector has since been implemented alongside Google Drive (`feature/kan-2-connectors`). It is treated as a seventh, optional source; onboarding UI support and authority-tier signals for it land in a later PRD revision.

---

## 5. Users

**Launch wedge (see Section 3):** AI support agents at 50–500 person B2B SaaS companies running Zendesk. The personas below are described generally, but launch messaging, onboarding defaults, and the demo all target the customer-support use case first.

### Primary — AI engineers deploying support or ops agents

These are the people who keep hitting the "agent doesn't know our process" wall. At a 50–500 person B2B SaaS company, this is typically one person: the engineer who owns the agent stack. They have a tooling budget, they can self-serve an API integration, and they do not need procurement approval.

**Pain:** Writing and maintaining bespoke prompt context for every policy edge case. When policy changes, they have to find every prompt it touches and update each manually. When an edge case breaks the agent, they have to diagnose it manually and patch it by hand.

**How they use Company Brain:** Connect sources during onboarding. Point MCP server at their agent framework. The agent calls `query_brain(situation)` before each task. Brain returns the matched skill with decision logic and actions. Done.

### Secondary — CS or ops team leads who manage the review queue

When the extraction engine produces a low-confidence skill or detects a contradiction between sources, it goes to the review queue instead of auto-publishing. This person reviews proposed changes, sees the source context, and approves, rejects, or corrects. They are not technical.

**How they use Company Brain:** Simple review UI. See the proposed change alongside the source it came from. Make a decision in under 30 seconds per item. The UI is designed so they never need to understand the extraction system — just the content.

---

## 6. Architecture

Company Brain is organized into three layers. The onboarding sweep populates the brain at signup. The event-driven pipeline keeps it current thereafter. The delivery layer serves skills to agents.

```
┌─────────────────────────────────────────────────────────────────┐
│  SOURCES                                                        │
│  Slack · Notion · Google Drive · GitHub · Jira · Zendesk        │
│  Connected via MCP clients + webhook subscriptions              │
│  ─────────────────────────────────────────────────────────────  │
│  AGENT RUNS (successful traces: tool calls, files, scripts)     │
│  Pushed by the agent harness via POST /runs  ─────────────┐     │
└───────────────────────────┬───────────────────────────────┼─────┘
                            │                               │
              ┌─────────────┴──────────────┐                │
              │ ONBOARDING SWEEP           │ EVENT-DRIVEN   │
              │ (one-time, at signup)      │ (ongoing)      │
              └─────────────┬──────────────┘                │
                            │                               │
┌───────────────────────────▼───────────────────────────────┼─────┐
│  EXTRACTION ENGINE                                        │     │
│  Relevance gate → Context expansion → Two-pass extraction │     │
│  → Boundary classification → Contradiction detection      │     │
│  → Confidence scoring → Skill writing + embedding         │     │
│  ─────────────────────────────────────────────────────────┘     │
│  Agent-run lane: success gate → trajectory compression →        │
│  procedure extraction → (same boundary/contradiction/write)     │
└───────────────────────────┬─────────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────────┐
│  KNOWLEDGE CORE                                                 │
│  PostgreSQL + pgvector  ·  Redis cache  ·  Review queue        │
└───────────────────────────┬─────────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────────┐
│  DELIVERY                                                       │
│  FastMCP (port 8001)  ·  FastAPI REST (port 8000)              │
│  query_brain tool  ·  Query-driven extraction fallback         │
└───────────────────────────┬─────────────────────────────────────┘
                            │
                     AI AGENTS ──── successful run traces ────┐
                            ▲                                 │
                            └─────── self-improving loop ◄─────┘
```

### Living Currency

Every source connected during onboarding also has a webhook subscription created for ongoing monitoring. When a relevant event occurs in a monitored Slack channel, Notion space, Google Drive folder, or Jira project, the extraction engine re-processes only the affected content. Updated skills publish within five minutes of the source event.

### Self-Improving Loop

The delivery layer is also an ingestion surface. When an agent finishes a task successfully, its harness pushes the full run trace — tool calls, file exploration, scripts written, commands run, final outcome — to `POST /runs`. Runs that clear the success gate (Feature 30) are distilled by the same extraction engine into **procedure skills**: ordered, executable steps with the dead ends removed, reviewed by a human before any agent can act on them.

**The loop starts before signup.** Most teams arrive with months of agent history already sitting in LangSmith, Langfuse, an OTel store, or Claude Code transcripts. Run backfill (Feature 29a) imports that history through the same gate and distillation path, so a company gets its existing procedures on day one instead of waiting for three fresh runs of each task. It is the strongest cold-start asset the product has — see Section 13, Step 3.

The loop:

```
[cold start] import historical runs → same gate → same distillation
agent queries brain → executes task → run succeeds
  → trace pushed to /runs → success gate → trajectory compression
  → procedure extraction → review queue → human approves
  → published procedure skill
  → next agent asking the same thing gets the shortest known path
     (fewer exploration steps, fewer tokens, fewer wrong turns)
```

Two properties keep this honest and distinguish it from an agent-memory store:

- **Nothing is served unreviewed.** A distilled procedure enters the review queue like any other extraction; agent-run authority never auto-publishes (Feature 32). The trace is evidence, not truth.
- **We keep the procedure, not the episode.** Raw traces are redacted at ingest and expire on a retention window (default 30 days). The durable artifact is the versioned skill with its provenance back to the run id. See Section 4.

### Hybrid Retrieval

For every agent query:

1. Embed the situation string
2. Check Redis cache — hit: return immediately
3. pgvector cosine similarity search over published skill embeddings → top 5 candidates
4. If max similarity ≥ 0.70: return best match
5. If max similarity < 0.70: trigger query-driven extraction from live sources

---

## 7. Tech Stack

| Component          | Technology                    | Notes                                                                    |
| ------------------ | ----------------------------- | ------------------------------------------------------------------------ |
| API server         | FastAPI (Python)              | REST endpoints port 8000. Extraction engine lives here.                  |
| MCP server         | FastMCP                       | `query_brain` tool on port 8001. SSE transport.                          |
| Database           | PostgreSQL 16 + pgvector      | Skills registry, versioning, review queue, event log.                    |
| Vector index       | pgvector IVFFlat              | Cosine similarity on 1536-dim skill embeddings.                          |
| Cache              | Redis 7                       | 5-min TTL on search results. Invalidated on publish/update.              |
| Fast classifier    | Groq (`llama-3.3-70b-versatile`) | Relevance gate, decision moment identification, boundary classification. 10–20× faster than Haiku at lower cost. Sufficient for binary/four-way labels. |
| Skill extractor    | Claude Sonnet                 | Pass 2 extraction + contradiction detection. Quality-sensitive: produces the skill document agents act on. |
| Embeddings         | OpenAI text-embedding-3-small | 1536 dimensions. Skills table + agent query embedding.                   |
| Container runtime  | Docker + Docker Compose       | All services. Single compose file.                                       |
| Review UI          | FastAPI + Jinja2              | Minimal HTML. No frontend framework needed for MVP.                      |
| Agent demo         | Anthropic Python SDK          | Claude calls FastMCP directly. No orchestration framework.               |

---

## 8. Skill Format

The output of every extraction is a structured markdown document. This is what agents receive when they call `query_brain`. It is designed to be both human-readable (for the review queue) and machine-readable (for agents).

```markdown
# Skill: {skill_name}

## Trigger

{Plain English description of when this skill applies.
Written in "when-to-invoke" framing for semantic search accuracy.}

## Base Logic

IF {condition_a} → {action}
IF {condition_b} → {action}
IF {condition_c} → escalate to {role}

## Exceptions

| Condition   | Override       | Source | Authority       | Date   |
| ----------- | -------------- | ------ | --------------- | ------ |
| {condition} | {what changes} | {url}  | high/medium/low | {date} |

## Actions

- {action_name}({params}) → {what it does}
- {action_name}({params}) → {what it does}

## Source

Primary: {source_url} ({author}, {date})
Authority: {high | medium | low}

## Metadata

Version: {n}
Confidence: {0.0–1.0}
Status: {published | pending_review | draft | archived}
Last updated: {timestamp}
Changed by: {system | human_authored | {reviewer_id}}
```

### Why this format

Skills are stored and served as structured text, not as graph nodes or JSON blobs. Agents read and follow markdown instructions reliably. The exceptions table is the most important structural decision — it means most new policy extractions append a row to an existing skill's exception block rather than creating a new skill, which keeps the registry coherent and queryable.

### Procedure skills (agent-run sourced)

Skills distilled from successful agent runs (Features 29–34) use the **same format** — no second schema, no second retrieval path, no second review UI. The fields carry procedural meaning:

| Field | Policy skill | Procedure skill (agent-run sourced) |
| --- | --- | --- |
| `Trigger` | when this policy applies | the task the run accomplished, in when-to-invoke framing ("when asked to rotate a leaked API key") |
| `Base Logic` | IF/THEN decision rules | the **ordered minimal step sequence** that produced the outcome, with dead ends and retries removed |
| `Exceptions` | policy overrides | failure branches actually encountered in the run — what went wrong, what recovered it |
| `Actions` | actions the agent may take | the concrete tool calls / commands used, with their real parameter shapes |
| `Source` | source URL + author | `agent_run:{run_id}` + the agent identity, task, and run count backing it |

Two extra metadata lines appear on procedure skills:

```markdown
## Metadata

Origin: agent_run
Runs observed: {n}          # successful runs that agreed on this procedure
Median steps saved: {n}     # steps in observed runs − steps in this procedure
```

The exceptions table is what makes this compound rather than accumulate: the second successful run of the same task that took a different branch appends a row instead of creating a rival skill.

---

## 9. Data Model

### `skills`

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE skills (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name              VARCHAR(255) UNIQUE NOT NULL,
  version           INTEGER DEFAULT 1,
  trigger           TEXT,
  base_logic        TEXT,
  exceptions_block  JSONB DEFAULT '[]',
  -- [{condition, override, source_url, source_authority, author, date}]
  actions           JSONB DEFAULT '[]',
  -- [{name, params, description}]
  source_ids        JSONB DEFAULT '[]',
  source_authority  VARCHAR(10),
  -- high | medium | low  (authority of the primary backing source)
  origin            VARCHAR(20) DEFAULT 'source_extraction',
  -- source_extraction | agent_run | human
  -- agent_run → procedure skill distilled from successful run traces (Feature 32)
  run_support       JSONB DEFAULT '{}',
  -- {run_ids: [...], runs_observed: n, median_steps_observed: n, median_steps_published: n}
  -- populated only when origin = agent_run
  conflict_flags    JSONB DEFAULT '[]',
  status            VARCHAR(20) DEFAULT 'draft',
  -- draft | pending_review | published | archived
  confidence        FLOAT,
  embedding         VECTOR(1536),
  changed_by        VARCHAR(50),
  -- system | human_authored | {reviewer_id}
  created_at        TIMESTAMP DEFAULT NOW(),
  updated_at        TIMESTAMP DEFAULT NOW()
);

CREATE INDEX ON skills USING ivfflat (embedding vector_cosine_ops)
  WITH (lists = 100);
CREATE INDEX ON skills (status);
CREATE INDEX ON skills (source_authority);
```

> **Multi-tenancy note:** the schema above is shown single-tenant for readability. In the implemented multi-tenant schema every table carries a `workspace_id` with row-level security enforced, and skill names are unique per workspace — `UNIQUE (workspace_id, name)` — not globally.

### `skill_versions`

Full change history. Every write to a published skill creates a row here before updating the parent record.

```sql
CREATE TABLE skill_versions (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  skill_id          UUID REFERENCES skills(id),
  version           INTEGER,
  base_logic        TEXT,
  exceptions_block  JSONB,
  confidence        FLOAT,
  changed_by        VARCHAR(50),
  change_type       VARCHAR(30),
  -- update | exception_added | human_edit | sweep_sourced
  created_at        TIMESTAMP DEFAULT NOW()
);
```

### `review_queue`

Human-in-the-loop gate for low-confidence extractions, contradictions, and sweep-sourced skills.

```sql
CREATE TABLE review_queue (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  skill_id        UUID REFERENCES skills(id),
  review_type     VARCHAR(30),
  -- update | exception | contradiction | new | query_driven | sweep_sourced | procedure
  proposed_update JSONB,
  -- the full proposed skill body
  source_a        JSONB,
  -- {url, author, timestamp, excerpt, authority, tier}
  source_b        JSONB,
  -- populated only for review_type = contradiction
  confidence      FLOAT,
  reason          TEXT,
  status          VARCHAR(20) DEFAULT 'pending',
  -- pending | approved | rejected | human_written
  resolved_by     VARCHAR(50),
  created_at      TIMESTAMP DEFAULT NOW(),
  resolved_at     TIMESTAMP
);

CREATE INDEX ON review_queue (status);
CREATE INDEX ON review_queue (review_type);
```

### `source_events`

Log of every incoming webhook event. Used for sweep resume, audit trail, and debugging extraction failures.

```sql
CREATE TABLE source_events (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  source       VARCHAR(50),
  -- slack | notion | google_drive | github | jira | zendesk | agent_run
  event_type   VARCHAR(50),
  -- message | page_update | file_update | pr_merged | issue_closed | ticket_resolved
  -- | run_succeeded (agent_run: source_id = agent_runs.id)  | etc
  source_id    VARCHAR(255),
  payload      JSONB,
  processed    BOOLEAN DEFAULT FALSE,
  skill_id     UUID,
  -- set after extraction if a skill was created or updated
  outcome      VARCHAR(30),
  -- published | queued | discarded | duplicate | contradiction | failed
  -- failed = pipeline dead-letter after exhausted retries; re-runnable
  sweep_id     UUID,
  -- set if this event was processed during an onboarding sweep
  created_at   TIMESTAMP DEFAULT NOW()
);

CREATE INDEX ON source_events (source, processed);
CREATE INDEX ON source_events (sweep_id);
```

### `sweeps`

Tracks onboarding and manual sweep jobs.

```sql
CREATE TABLE sweeps (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  status         VARCHAR(20) DEFAULT 'running',
  -- running | completed | failed | paused
  config         JSONB,
  -- the source_authority.yaml contents at time of sweep
  progress       JSONB DEFAULT '{}',
  -- {source: {total, processed, published, queued, discarded}}
  skills_created INTEGER DEFAULT 0,
  skills_queued  INTEGER DEFAULT 0,
  started_at     TIMESTAMP DEFAULT NOW(),
  completed_at   TIMESTAMP
);
```

### `agent_interactions`

Logged on every `query_brain` call. Drives the MVP-minimal feedback loop (Feature 15a): a `human_override = true` interaction decrements the matched skill's confidence and, below the auto-publish floor, creates a review queue item. Richer episodic processing lands in v2.

```sql
CREATE TABLE agent_interactions (
  id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  skill_id           UUID REFERENCES skills(id),
  query              TEXT,
  matched_confidence FLOAT,
  match_type         VARCHAR(20),
  -- semantic | query_driven | no_match
  agent_action       JSONB,
  human_override     BOOLEAN DEFAULT FALSE,
  run_id             UUID REFERENCES agent_runs(id),
  -- set when the querying agent later reported the run this query fed into;
  -- links a published skill to the outcomes it produced (Feature 34)
  created_at         TIMESTAMP DEFAULT NOW()
);
```

### `agent_runs`

Traces of agent executions pushed by customer agent harnesses (Feature 29). The raw trace is the *evidence* for a distilled procedure skill, not a durable product artifact: `trace` is redacted at ingest and nulled by the retention job (`agent_runs.retention_days`, default 30) once distillation has completed. `trace_digest` and the resulting skill outlive it.

```sql
CREATE TABLE agent_runs (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  external_id     VARCHAR(255),
  -- caller's own run id; UNIQUE (workspace_id, external_id) for idempotent re-push
  agent_name      VARCHAR(255),
  -- logical agent identity, e.g. "support-triage-agent" — the clustering key with task
  task            TEXT,
  -- the goal string the agent was given
  task_embedding  VECTOR(1536),
  -- used to cluster runs of the same task before distillation (Feature 31)
  outcome         VARCHAR(20),
  -- success | failure | partial | unknown  (caller-reported)
  outcome_signals JSONB DEFAULT '{}',
  -- {human_confirmed, tests_passed, ticket_resolved, no_override, error_free_tail}
  eligible        BOOLEAN,
  -- success gate verdict (Feature 30); NULL until gated
  ineligible_reason VARCHAR(50),
  -- not_successful | too_trivial | too_noisy | redaction_failed | duplicate_of_run | policy_excluded
  trace           JSONB,
  -- normalized step envelope (see Feature 29); NULLed by retention job
  trace_digest    JSONB DEFAULT '{}',
  -- {step_count, tool_histogram, tokens, cost_usd, duration_ms, files_touched} — survives retention
  step_count      INTEGER,
  tokens_used     INTEGER,
  duration_ms     INTEGER,
  harness         VARCHAR(50),
  -- claude_code | agent_sdk | openai | langsmith | langfuse | otel | custom
  -- (the adapter that normalized it)
  ingest_mode     VARCHAR(10) DEFAULT 'live',
  -- live | backfill  (backfill = imported run history, Feature 29a)
  sweep_id        UUID,
  -- set for backfilled runs; reuses the sweeps table for progress + resume
  cluster_id      UUID,
  -- set when the run joins a task cluster awaiting the distillation threshold
  skill_id        UUID REFERENCES skills(id),
  -- set once this run contributed to a distilled procedure skill
  distilled_at    TIMESTAMP,
  created_at      TIMESTAMP DEFAULT NOW()
);

CREATE INDEX ON agent_runs (agent_name, outcome);
CREATE INDEX ON agent_runs (cluster_id) WHERE distilled_at IS NULL;
CREATE INDEX ON agent_runs USING ivfflat (task_embedding vector_cosine_ops)
  WITH (lists = 100);
```

> **Tenancy and privacy.** `agent_runs` carries `workspace_id` under RLS like every other table. Traces are the most sensitive payload in the system — they contain file contents, shell output, and customer records — so ingest runs a secret/PII redaction pass (Feature 29) *before* the row is written, and no endpoint ever returns a raw trace to an agent credential. Only the distilled skill is served.

### `source_connections`

OAuth token storage and monitored channel/space/project configuration. Created during onboarding. The `monitored_ids` field drives both the sweep scope and ongoing webhook monitoring — stored here instead of `source_authority.yaml` so changes take effect immediately without a process restart.

```sql
CREATE TABLE source_connections (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  source           VARCHAR(50) UNIQUE NOT NULL,
  -- slack | notion | google_drive | github | jira | zendesk
  status           VARCHAR(20) DEFAULT 'connected',
  -- connected | disconnected | error
  access_token     TEXT,
  refresh_token    TEXT,
  token_expires_at TIMESTAMP,
  monitored_ids    JSONB DEFAULT '[]',
  -- channel/space/project IDs selected during onboarding
  lookback_days    INTEGER DEFAULT 180,
  connected_at     TIMESTAMP DEFAULT NOW(),
  updated_at       TIMESTAMP DEFAULT NOW()
);

CREATE INDEX ON source_connections (source, status);
```

### `webhook_subscriptions`

Tracks every active webhook subscription created during onboarding. Required for lifecycle management: when a source is disconnected or a monitored channel is removed, subscriptions must be explicitly revoked. Without this table there is no way to know what to un-subscribe.

**Note on Google Drive:** Drive delivers events via push-notification channels (watch on a folder/file via the Changes API) that **expire** (max ~7 days) and must be renewed. The `expires_at` column below drives a renewal job; the other sources' subscriptions do not expire and leave it null.

```sql
CREATE TABLE webhook_subscriptions (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  source          VARCHAR(50),
  source_ref_id   VARCHAR(255),
  -- ID the source system assigned to this subscription
  target_id       VARCHAR(255),
  -- channel/space/project being monitored
  status          VARCHAR(20) DEFAULT 'active',
  -- active | revoked | expired
  expires_at      TIMESTAMP,
  -- set for sources with expiring channels (Google Drive); null otherwise
  created_at      TIMESTAMP DEFAULT NOW()
);

CREATE INDEX ON webhook_subscriptions (source, status);
CREATE INDEX ON webhook_subscriptions (expires_at);
```

---

## 10. Source Authority Config

Loaded from `source_authority.yaml` at startup. Contains only static configuration: tier weights, routing thresholds, and sweep settings. Version-controlled alongside the codebase.

**The `monitored` section is not in this file.** Which channels, spaces, and projects to monitor is stored in `source_connections.monitored_ids` (populated during onboarding). The config loader merges both at runtime. This avoids requiring a process restart when onboarding adds a new monitored channel.

```yaml
# source_authority.yaml

tiers:
  high:
    weight: 1.0
    sources:
      - type: notion
        signals: [designated_policy_page, owner_edited]
      - type: google_drive
        signals: [designated_policy_folder, owner_edited]
      - type: github
        signals: [path_prefix=/docs, path_prefix=/runbooks]
      - type: jira
        signals: [ticket_type=policy, status=done]

  medium:
    weight: 0.7
    sources:
      - type: slack
        signals: [channel=policy, channel=ops-decisions, channel=cs-escalations, channel=engineering-decisions]
      - type: zendesk
        signals: [tag=policy-exception, status=solved]
      - type: agent_run
        signals: [human_confirmed]
        # a run a human explicitly confirmed as correct — the only agent-run
        # signal that earns medium. Still never auto-publishes (see agent_runs below).

  low:
    weight: 0.4
    sources:
      - type: slack
        signals: []
      - type: agent_run
        signals: []
        # machine-judged success (tests passed, ticket resolved, no override)
      - type: google_drive
        signals: [outside_designated_folder]
      - type: github
        signals: [content_type=comment]
      - type: jira
        signals: [content_type=comment]

routing:
  auto_publish_confidence: 0.90
  auto_publish_authority_floor: medium
  review_queue_confidence_floor: 0.70
  # below 0.70 → draft, not surfaced to reviewers

sweep:
  processing_order: [notion, google_drive, github, jira, slack, zendesk]
  rate_per_minute: 10    # items processed per source per minute
  semaphore_limit: 5     # concurrent LLM calls
  auto_publish_during_sweep: false
  # all sweep extractions go to review_queue regardless of confidence
  fast_track_high_tier: true
  # high-tier designated sources (Notion policy pages, Drive policy folders)
  # surface first in the review queue with a lightweight one-click "verify"
  # flow instead of the full review card — see Onboarding Flow, Step 3

agent_runs:
  enabled: true
  auto_publish: false
  # NON-NEGOTIABLE. A distilled procedure always goes to the review queue,
  # regardless of confidence. A machine-judged success is evidence, not approval.
  success_gate:
    require_outcome: success
    require_no_override: true      # no human_override on the linked agent_interaction
    require_error_free_tail: true  # last 3 steps contain no failed tool call
    min_steps: 3                   # below this the run is trivial; nothing to learn
    max_steps: 400                 # above this the trace is too noisy to compress reliably
  distillation:
    min_runs_per_cluster: 3
    # distil only once N successful runs agree on a task — one lucky run is an anecdote.
    # Overridden to 1 when the run is human_confirmed.
    cluster_similarity: 0.85       # task-embedding cosine threshold for "same task"
    max_distillations_per_day: 50  # cost ceiling per workspace
  retention:
    raw_trace_days: 30             # trace JSONB nulled after this; digest + skill persist
    redact_secrets: true           # never storable: tokens, keys, auth headers, env values
  backfill:                        # Feature 29a — importing pre-existing run history
    enabled: true
    lookback_days: 90              # older runs may reference decommissioned tools
    infer_outcome_when_missing: true
    # Groq classifies terminal state from the trace tail; "ambiguous" is excluded,
    # never guessed into eligibility. Inferred runs are capped at low authority.
    max_clusters_per_sweep: 200
    cluster_order: frequency_desc  # biggest traffic first, then newest
    import_batch_size: 10000       # NDJSON envelopes per POST /runs/import job
```

---

## 11. Features

### Layer 1 — Source Connections

**Feature 1 — Webhook receiver**
`POST /ingest/event` receives incoming events from all connected sources. Validates the payload, logs to `source_events`, and enqueues for extraction processing.

Supported event types per source:

| Source  | Event types                                                                   |
| ------- | ----------------------------------------------------------------------------- |
| Slack   | `message` in monitored channels, `message_changed` (edits), `pin_added`       |
| Notion  | `page.updated`, `page.created` in monitored spaces                            |
| Google Drive | `file.created`, `file.updated` (via Drive changes/push notifications) in monitored folders |
| GitHub  | `pull_request.closed` (merged), `issue.closed`, `push` to monitored paths     |
| Jira    | `issue.updated` (status transitions), `comment.created` on monitored projects |
| Zendesk | `ticket.updated` (solved), `ticket.tagged` with monitored tags                |

**Feature 2 — Context expanders**
One expander per source. Each knows how to fetch full surrounding context for an event, not just the triggering message. Called during extraction after the relevance gate passes.

| Source  | What the expander fetches                                            |
| ------- | -------------------------------------------------------------------- |
| Slack   | Full thread from the message ID, including all replies and reactions |
| Notion  | Full page body + parent page title + linked page titles and excerpts |
| Google Drive | Full document text (Docs/Sheets/Slides exported to text) + parent folder name + document comments |
| GitHub  | PR description + all review comments + body of linked issues         |
| Jira    | Ticket body + all comments + linked tickets + transition log         |
| Zendesk | Ticket + all comments + tags + resolution note                       |

**Feature 3 — Source authority annotator**
Reads `source_authority.yaml`, determines the tier of an incoming event or sweep item based on source type and signals, and annotates the context before passing it to extraction. The tier is passed explicitly to the extraction prompt so the LLM knows how to weight conflicting information.

---

### Layer 2 — Extraction Engine

**Feature 4 — Relevance gate** (Groq fast classifier)
Single LLM call. Binary question: does this content contain operational decision logic, a policy rule, a process instruction, or an exception to an existing rule? Yes → proceed. No → mark `source_events.outcome = discarded`. This is the cheapest call in the pipeline and filters most noise before any expensive processing.

**Feature 5 — Pass 1: Decision moment identifier** (Groq fast classifier)
For threaded or long-form sources (Slack threads, Jira comment chains, GitHub review threads): reads the full content and identifies only the authoritative decision moments.

Signals it looks for:

- Definitive language: "going forward", "the policy is", "from now on", "confirmed:", "final answer:", "we've decided"
- Positive reactions from multiple people (✅, 👍) indicating consensus
- Pinned messages (always treated as authoritative)
- @channel or @here announcements in policy channels
- Messages from designated authorities (CS lead, Head of Operations, etc.)
- Recency among debated messages (later messages often resolve earlier disagreement)

Output: `[{message_id, author, timestamp, decision_text, signals}]`

**Feature 6 — Pass 2: Skill extractor** (Claude Sonnet)
Takes decision moments from Pass 1 plus authority-annotated full context. Extracts a structured skill draft. The prompt instructs the model to prioritize higher-authority sources when sources conflict and to express uncertainty rather than hallucinate.

Output:

```json
{
  "trigger": "...",
  "base_logic": "...",
  "exceptions": [...],
  "actions": [...],
  "extraction_confidence": 0.0-1.0,
  "uncertainty_notes": "..."
}
```

**Feature 7 — Boundary classifier** (Groq fast classifier)
After extraction, runs a pgvector cosine search over published skills (top 3 matches). If max similarity > 0.82, sends the new extraction alongside the matching skill to the fast classifier with a four-way classification question:

> **Sweep-scope rule:** during an onboarding sweep nothing is published yet (`auto_publish_during_sweep = false`), so a published-only search would classify every item as NEW and flood the review queue with duplicates. When processing sweep items, the similarity search runs over **published + `pending_review`** skills.

- **UPDATE** — this changes the base logic of the existing skill
- **EXCEPTION** — this is a new exception or override to add to the existing skill
- **DUPLICATE** — this says the same thing the existing skill already says
- **NEW** — this is genuinely distinct from all existing skills

Routes by result:

- UPDATE → propose a version diff on base_logic, send to contradiction detector
- EXCEPTION → append a row to the exceptions_block, send to confidence scorer
- DUPLICATE → discard, log source_id as already covered
- NEW → create new skill entry, send to contradiction detector

**Feature 8 — Contradiction detector**
Runs when the boundary classifier returns UPDATE or NEW. Compares the proposed change against the current published skill (during a sweep, against the matched `pending_review` skill — same sweep-scope rule as Feature 7). If the proposed content conflicts with the existing content on the same condition:

- Does not update the skill
- Does not create a new skill
- Creates a `review_queue` row with `review_type = contradiction`, populating both `source_a` and `source_b` with full source metadata
- Marks `source_events.outcome = contradiction`

**Feature 9 — Confidence scorer**
Computes the final confidence score used for routing decisions.

```
base_score = extraction_confidence (Sonnet self-report, 0.0–1.0)

authority_multiplier:
  high   → 1.0
  medium → 0.85
  low    → 0.65

final_confidence = base_score × authority_multiplier

overrides:
  contradiction_detected = true  → confidence = 0.0 (forces review)
  changed_by = human_authored    → confidence = 1.0 (bypasses routing)
  sweep_sourced = true           → route to review_queue regardless of score
```

**Feature 10 — Skill writer**
Takes the extraction output and confidence score. Writes or updates the skills table. Creates a `skill_versions` row on every change to a published skill. Routes by confidence:

```
confidence ≥ 0.90
  AND no contradiction
  AND source_authority ≥ medium
  AND sweep_sourced = false
  → auto-publish, version bump, Redis cache invalidate

confidence 0.70–0.89
  OR source_authority = low
  OR sweep_sourced = true
  → status = pending_review, write to review_queue

confidence < 0.70
  → status = draft, logged to source_events, not shown in review queue
```

**Feature 11 — Embedder**
Calls OpenAI `text-embedding-3-small` on the skill's `trigger` + `base_logic` text. Stores the resulting 1536-dimension vector in `skills.embedding`. Called after Pass 2 extraction and **before** boundary classification, which needs the vector to search (see Process 1); re-run whenever skill_writer commits a content change. Also called at query time to embed the agent's situation string for semantic search.

---

### Layer 3 — Knowledge Core

**Feature 12 — pgvector semantic search**
IVFFlat index on `skills.embedding`. Cosine similarity search over published skills. Used by both the boundary classifier (searching for related existing skills) and the `query_brain` delivery tool (finding the best match for an agent query). Returns top-k results with similarity scores.

**Feature 13 — Redis search cache**
Five-minute TTL on `query_brain` search results, keyed by the query embedding hash. Invalidated immediately on any skill publish, version bump, or status change. Reduces LLM embedding calls and database load for repeated or similar agent queries.

**Feature 14 — Skill versioning**
Every update to a published skill creates a `skill_versions` row capturing the previous state, the change type, and the `changed_by` field. The parent skills row then updates with the new content and an incremented version number. The full version history is accessible via `GET /skills/{id}/versions`.

---

### Layer 4 — Delivery

**Feature 15 — `query_brain` MCP tool**
The primary interface for AI agents. Full implementation:

1. Embed the incoming situation string (OpenAI)
2. Check Redis cache — hit: return immediately
3. pgvector cosine search → top 5 published skills by similarity
4. If max similarity ≥ 0.70: return best match
5. If max similarity < 0.70: trigger Feature 16 (query-driven extraction)
6. Cache result in Redis (5-min TTL)
7. Log to `agent_interactions`

**Feature 15a — Feedback loop (MVP-minimal)**
Closes the loop between agent usage and skill quality without waiting for v2 episodic processing:

- Agent frameworks (or humans supervising them) can report an override via `POST /interactions/{id}/override`
- On override: decrement the matched skill's confidence by 0.05 (floor 0.0)
- If the skill's confidence falls below 0.90 (the auto-publish floor), create a `review_queue` row with `review_type = update` and `reason = "agent override reported"`
- Override rate per skill is surfaced in the review UI so reviewers see which skills are failing in production

Response schema:

```json
{
  "skill_name": "Refund Request Handling",
  "trigger": "Customer submits a refund request",
  "base_logic": "IF order_age <= 30 days → approve...",
  "exceptions": [
    {
      "condition": "Item damaged in transit",
      "override": "Approve regardless of time window",
      "source_url": "https://slack.com/...",
      "authority": "medium",
      "date": "2025-03-12"
    }
  ],
  "actions": [
    { "name": "approve_refund", "params": ["order_id"] },
    { "name": "reject_refund", "params": ["order_id", "reason"] },
    { "name": "escalate_to_human", "params": ["order_id", "assignee"] }
  ],
  "confidence": 0.94,
  "source_authority": "high",
  "version": 3,
  "match_type": "semantic",
  "similarity_score": 0.91
}
```

**Feature 16 — Query-driven extraction**
When `query_brain` finds no match (max similarity < 0.70), instead of returning empty:

1. Use the situation string as a search query against source MCPs (Notion, Google Drive designated folders, Slack designated channels, Zendesk)
2. Fetch top results from connected sources
3. Run full extraction pipeline on combined results (same as event-driven)
4. Return the extracted skill to the agent immediately, flagged as `match_type: query_driven`
5. Store as draft in `review_queue` with `review_type: query_driven` for human promotion to published
6. If the agent interaction is later marked as successful (no override, no correction), confidence bumps on next review

**Latency budget:** query-driven extraction runs a multi-step LLM pipeline inline in an agent call. Hard budget: **15 seconds**. If the pipeline cannot complete within budget, return immediately with `{match_type: "no_match", extraction_queued: true, retry_after_seconds: 60}` and finish the extraction asynchronously — the agent can retry or escalate to a human. Agent integration docs must state this contract explicitly so developers can set their own timeouts sanely.

> **Implementation status (July 2026):** the latency/async-fallback contract, interaction logging, and a provider-search seam (`app/pipeline/query_extraction.py::search_sources`) are implemented and tested. The remaining piece is the per-source live search itself — see the Phase 5 implementation-status note in Section 16 for the design and build order.

**Feature 17 — FastAPI REST API**

See Section 14 for full endpoint reference.

---

### Review System

**Feature 18 — Review queue list**
`GET /review` returns pending items grouped by type. Contradictions are shown first (highest risk if unresolved). Each item shows: skill name, review type, source authority, confidence, source name, and age.

**Feature 19 — Update review card**
For `review_type = update`. Shows the existing skill's base_logic alongside the proposed change as a side-by-side diff. Approve: bumps version, publishes, invalidates cache. Reject: discards the proposed update, logs outcome.

**Feature 20 — Exception review card**
For `review_type = exception`. Shows the existing skill and the single proposed row to append to the exceptions table. Approve: appends the row, bumps version. Reject: discards.

**Feature 21 — Contradiction review card**
For `review_type = contradiction`. The most critical card. Shows both conflicting sources side by side with full metadata, and the current published skill state.

```
┌─────────────────────────────────────────────────────────┐
│ ⚠️ Contradiction: Refund Request Handling               │
├──────────────────┬──────────────────────────────────────┤
│ SOURCE A         │ SOURCE B                             │
│ Notion [HIGH]    │ Slack #cs-policy [MEDIUM]            │
│ Apr 1, 2025      │ Mar 15, 2025                         │
│ Sarah Chen       │ Mike Torres                          │
│                  │                                      │
│ "Approve refunds │ "30 day hard limit,                  │
│  up to 45 days   │  no exceptions for                   │
│  for premium"    │  premium customers"                  │
│                  │                                      │
│ [Open in Notion] │ [Open in Slack]                     │
├──────────────────┴──────────────────────────────────────┤
│ CURRENT SKILL SAYS: 30 day hard limit                   │
├─────────────────────────────────────────────────────────┤
│ [Source A is correct]  [Source B is correct]            │
│ [Neither — I'll write the correct version]              │
└─────────────────────────────────────────────────────────┘
```

Outcomes:

- Pick Source A or B → that source becomes the authoritative basis, skill updates, `changed_by = reviewer_id`
- Write correction → reviewer writes the correct version directly, `changed_by = human_authored`, confidence = 1.0, auto-publishes

**Feature 22 — Query-driven review card**
For `review_type = query_driven`. Shows what the agent queried, the skill the live extraction produced, and the source it came from. Reviewer can promote to published, edit and promote, or discard.

**Feature 23 — Bulk review for sweep items**
During and after the onboarding sweep, the review queue will contain many `sweep_sourced` items. The bulk review UI lets reviewers approve or reject groups of pending skills by topic cluster rather than one at a time. Clusters are computed by grouping skills with pairwise similarity > 0.75. Reviewing the highest-confidence item in a cluster and selecting "apply to similar" approves all items in the cluster at once.

---

### Onboarding

**Feature 24 — Source connector setup**
OAuth connection flow for each source. Slack, Notion, Google Drive, GitHub, Jira, and Zendesk each have their own OAuth screen. Google Drive uses Google OAuth with the `drive.readonly` scope. Connection credentials are stored and used for both the onboarding sweep and ongoing webhook subscriptions.

**Feature 25 — Onboarding configuration UI**
Channel, space, and project picker with time window selector. The user explicitly chooses which sources contain operational knowledge. This configuration:

1. Drives the onboarding sweep (what to ingest)
2. Sets up webhook subscriptions (what to monitor going forward)
3. Populates the `monitored` section of `source_authority.yaml`

**Feature 26 — Sweep worker**
Background job that processes historical content in authority-priority order with rate limiting. Resumable: if the job fails, it picks up from the last processed item using the `source_events.sweep_id` field. Progress is tracked per source in the `sweeps` table.

Processing order: Notion → Google Drive → GitHub → Jira → Slack → Zendesk

```python
async def run_sweep(sweep_id: str):
    sources = get_ordered_sources(sweep_id)   # authority order

    for source in sources:
        items = await fetch_historical_items(source)
        await update_sweep_progress(sweep_id, source, total=len(items))

        async with asyncio.Semaphore(SEMAPHORE_LIMIT):
            for batch in chunks(items, size=10):
                await asyncio.gather(*[
                    process_item(item, sweep_id=sweep_id)
                    for item in batch
                ])
                await update_sweep_progress(sweep_id, source, processed=10)
                await asyncio.sleep(60 / SWEEP_RATE)
```

**Feature 27 — Sweep progress tracking**
`GET /ingest/sweep/{sweep_id}/status` returns real-time progress:

```json
{
  "status": "running",
  "sources": {
    "notion": {
      "total": 71,
      "processed": 71,
      "published": 0,
      "queued": 20,
      "discarded": 51
    },
    "github": {
      "total": 18,
      "processed": 18,
      "published": 0,
      "queued": 8,
      "discarded": 10
    },
    "slack": {
      "total": 401,
      "processed": 257,
      "published": 0,
      "queued": 31,
      "discarded": 226
    },
    "zendesk": {
      "total": 1240,
      "processed": 0,
      "published": 0,
      "queued": 0,
      "discarded": 0
    }
  },
  "skills_created": 59,
  "review_queue_count": 59,
  "started_at": "2025-05-16T09:00:00Z"
}
```

---

### Agent Demo

**Feature 28 — Claude agent demo** (`agent-demo/demo.py`)

End-to-end demonstration of the full system.

Scenario: `"Customer requesting refund, 38 days post-purchase, $200 order, claims item arrived damaged in transit"`

Expected flow:

1. Agent calls `query_brain(situation=...)`
2. Brain returns Refund Request Handling skill, version 3
3. Exceptions block contains: "Item damaged in transit → Approve regardless of time window"
4. Agent reads exception, overrides the 30-day base logic
5. Agent calls `approve_refund(order_id)` + `initiate_carrier_claim(order_id)`
6. Agent prints resolution grounded in the returned skill (name + version cited)
7. Interaction logged to `agent_interactions` with `match_type: semantic`

Acceptance: zero hallucinated policy in agent output. Every decision traceable to a published, human-reviewed skill.

---

### Layer 5 — Agent Run Ingestion (the self-improving loop)

Successful agent runs are treated as a source like Slack or Notion: same relevance gating, same boundary classifier, same contradiction detection, same review queue, same versioned skills. Only the front of the pipeline differs — a trace is not prose, so it needs its own eligibility gate and its own compression pass before Sonnet can write a skill from it. Everything downstream of Feature 32 is the existing engine, unmodified.

**Feature 29 — Run trace ingestion**
`POST /runs` accepts a run trace from a customer's agent harness. Authenticated by `X-API-Key` with a new `runs:write` scope (write-only: a `runs:write` credential cannot read runs back). Rate-limited and size-capped (1 MB body, 400 steps; oversize → `413` with a pointer to the step cap in config).

*Normalized trace envelope* — adapters translate each harness into this shape:

```json
{
  "externalId": "run_8f21",
  "agentName": "support-triage-agent",
  "task": "Refund a customer whose order shipped damaged past the 30-day window",
  "outcome": "success",
  "outcomeSignals": { "humanConfirmed": true, "ticketResolved": true, "testsPassed": null },
  "startedAt": "2026-08-01T10:04:00Z",
  "steps": [
    { "index": 0, "type": "tool_call",   "name": "query_brain", "args": {...}, "status": "ok", "latencyMs": 240 },
    { "index": 1, "type": "file_read",   "name": "policies/refunds.md", "resultDigest": "…", "status": "ok" },
    { "index": 2, "type": "shell",       "name": "python scripts/check_order.py 8821", "resultDigest": "…", "status": "error" },
    { "index": 3, "type": "shell",       "name": "python scripts/check_order.py --env=prod 8821", "status": "ok" },
    { "index": 4, "type": "tool_call",   "name": "approve_refund", "args": {...}, "status": "ok" },
    { "index": 5, "type": "assistant_message", "text": "Refund approved under the damaged-in-transit exception." }
  ]
}
```

Adapters ship for: **Claude Agent SDK / Claude Code** transcripts, OpenAI-style tool-call message lists, LangSmith runs, and raw OTel spans. Custom harnesses post the envelope directly.

> **Step-type addendum (v1.4.1, implemented).** The `type` enum is `tool_call | file_read | **file_write** | shell | assistant_message`. `file_write` was added during implementation: folding writes into `tool_call` erases the read/write distinction, and "read the policy, then edit the config" is a different procedure from "read the policy, then read the config" — precisely the signal Feature 31 compresses on. The client-side producer for this envelope is specced in `docs/AGENT_HOOK_SHIM.md`; `PostToolUse` is the hook that carries the trajectory, and it is the one every competitor's capture layer skips.

Ingest does four things before the row is written, in order:

1. **Redact** — secrets, tokens, auth headers, env values, and connection strings are stripped by pattern; step args and results over 2 KB are replaced by digests. Redaction failure marks the run `ineligible_reason = redaction_failed` and drops the trace body; it never stores the payload "just in case".
2. **Normalize + digest** — compute `trace_digest` (step count, tool histogram, tokens, cost, duration, files touched), which survives the retention window after the raw trace is nulled.
3. **Embed the task string** into `task_embedding` for clustering.
4. **Enqueue** the success gate on the ARQ worker and return `202` immediately. Ingestion never blocks the agent's critical path — a run push must cost the caller a single fast round-trip.

**Feature 29a — Run history backfill (cold start)**
Agent runs do not begin when a company connects Brainite — most teams arrive with months of trace history sitting in an observability tool. That history is the single best cold-start asset in the product: it is *already* labeled by outcome, already scoped to this company's real tasks, and it costs the customer nothing to hand over.

Backfill is the onboarding sweep's counterpart for traces, and reuses the same machinery: a `sweeps` row, the same rate limits and semaphore, the same review-queue destination.

Import paths, in build order:

| Where history lives | How we get it |
| --- | --- |
| **LangSmith / Langfuse** | API key + project id → paginate runs, filter to terminal successes, adapt to the step envelope |
| **Claude Code / Agent SDK transcripts** | Customer uploads or points us at their transcript store (`.jsonl` session files); the same adapter as live ingest |
| **OTel / trace store** | Span export (Jaeger/Tempo/Datadog) → spans with tool-call semantics adapted per convention |
| **Customer's own logs** | Bulk `POST /runs/import` (NDJSON, up to 10k envelopes per job) for anything homegrown |

Backfill differs from live ingest in four ways:

1. **Outcome labels are weaker.** Historical runs often lack an explicit success flag. Where the harness recorded none, a Groq call classifies terminal state from the trace tail (`success | failure | ambiguous`); `ambiguous` is excluded — never guessed into eligibility. Backfilled runs carry `outcome_signals.inferred = true` and are capped at **low** authority even if a human later confirms the skill's *text*, because nobody confirmed the *run*.
2. **Volume is lumpy.** A year of logs can be 100k runs. Clustering runs first and distilling **per cluster, highest-frequency first** keeps this bounded: the top 50 clusters usually cover the majority of an agent's traffic. `max_distillations_per_day` still applies; the sweep drains over days, newest clusters first (recent procedures beat stale ones).
3. **Staleness is real.** A procedure from a run 11 months ago may reference a decommissioned tool. Backfill applies a lookback window (default 90 days, configurable) and stamps each procedure skill with the age of its newest supporting run; the review card shows it.
4. **Everything lands in review.** Same rule as live: `origin = agent_run` never auto-publishes. The bulk review UI (Feature 23) clusters procedure cards the same way it clusters sweep items.

*Value framing:* this is what makes the loop credible at signup rather than at month three — "point us at your LangSmith project and get back the twenty procedures your agents already know" is the demo. It is also the honest answer to why the corpus is a moat: a competitor can copy the pipeline, but not the customer's run history.

**Feature 30 — Run success gate**
The relevance gate's counterpart for traces. Cheap, deterministic checks first (no LLM):

```
eligible = outcome == success
  AND no human_override on the linked agent_interaction
  AND last 3 steps contain no failed tool call
  AND min_steps <= step_count <= max_steps
  AND not a duplicate trace_digest of an already-distilled run
```

Failing runs are kept with `eligible = false` and an `ineligible_reason` (they are still useful: failure rates per agent are a product signal, and Section 15 tracks them), but never distilled. Eligible runs join a **task cluster** — cosine ≥ 0.85 on `task_embedding` against undistilled runs of the same `agent_name`. Distillation fires when a cluster reaches `min_runs_per_cluster` (default 3), or immediately for a `humanConfirmed` run. **This threshold is the cost control**: one Sonnet distillation per *task*, not per run.

*Why a threshold and not every run:* a single successful run is an anecdote — it may have succeeded through luck, a stale cache, or a path that only works for one customer. Three independent runs converging on the same spine is evidence of a procedure. It also caps spend: an agent running a task 500 times produces one distillation, not 500.

**Feature 31 — Trajectory compression** (Groq fast classifier)
Pass 1's counterpart for traces. Input: the cluster's traces. Output: the **causal spine** — the minimal ordered step sequence that actually produced the outcome.

It drops:

- Dead-end exploration (files read but never acted on; searches that returned nothing used)
- Retries and their failed predecessors — *but records the failure→recovery pair as a candidate exception*
- Steps present in only one run of the cluster (run-specific noise, not procedure)
- Redundant re-reads of the same file, verbose intermediate reasoning

It keeps: the steps common to all successful runs in the cluster, in order, plus the parameter shapes that mattered.

Output: `{spine: [{step, tool, purpose, params}], divergences: [...], recoveries: [{failure, fix}], steps_observed_median, steps_in_spine}`

**Feature 32 — Procedure extractor** (Claude Sonnet)
Pass 2's counterpart. Takes the compressed spine plus the task string and writes a skill in the standard format (Section 8): `trigger` = when to run this task, `base_logic` = the ordered steps, `exceptions` = the recovery pairs and cluster divergences, `actions` = the tool signatures used.

The prompt is explicitly constrained to prevent the failure mode that makes trace-learning dangerous — **generalizing from one company's accident into a stated rule**:

- Describe only what the runs did; never invent a step no run performed
- Do not state a policy the run merely *assumed* — if the run relied on a rule (a 30-day window), cite the skill or source it came from rather than restating it as fact
- Mark any step whose necessity is uncertain as an open question for the reviewer
- Emit `extraction_confidence` reflecting cluster agreement (unanimous spine → high; heavy divergence → low)

From here the run rejoins the standard pipeline unchanged: **embed → boundary classifier → contradiction detector → confidence scorer → skill writer**. Two rules apply on top:

- `origin = agent_run` forces review routing exactly as `sweep_sourced = true` does. **Agent-run skills never auto-publish**, at any confidence. This is the whole difference between us and an agent-memory store.
- If the boundary classifier returns UPDATE against an *existing* skill and the run's procedure contradicts a **human-authored** or **high-authority** skill, the result is a contradiction card, not an update. A machine's success does not overturn a human's policy.

**Feature 33 — Procedure review card**
For `review_type = procedure`. Shows the reviewer what an approval actually means:

```
┌──────────────────────────────────────────────────────────────┐
│ 🔁 Procedure learned: Rotate a leaked API key                │
│    support-triage-agent · 4 successful runs · 2 human-confirmed│
├──────────────────────────────────────────────────────────────┤
│ PROPOSED PROCEDURE          │ WHAT THE RUNS ACTUALLY DID     │
│ 1. Revoke key in dashboard  │ median 19 steps → 6 in spine   │
│ 2. Rotate secret in vault   │ 3/4 runs identical             │
│ 3. Redeploy affected svc    │ 1 run diverged at step 3 ▸     │
│ 4. Post to #security-log    │                                 │
├──────────────────────────────────────────────────────────────┤
│ EXCEPTIONS FOUND            │ OPEN QUESTIONS                  │
│ vault 429 → retry w/ backoff│ is step 4 required, or habit?   │
├──────────────────────────────────────────────────────────────┤
│ [Approve & publish] [Edit steps & publish] [Reject]          │
│ [View run 8f21 ▸] (raw trace, dashboard JWT only)            │
└──────────────────────────────────────────────────────────────┘
```

Reject requires a reason (`wrong | unsafe | too_specific | already_covered | not_a_procedure`) — this is the labeled data that tunes the gate and the compression prompt.

**Feature 34 — Run-outcome reinforcement**
The positive mirror of Feature 15a. When a pushed run cites a `query_brain` interaction (`agent_interactions.run_id`) and the run succeeded following that skill:

- Bump the skill's confidence by 0.02 (ceiling 1.0 for machine signal; only human review sets 1.0 outright)
- Increment `run_support.runs_observed` and refresh `median_steps_observed`
- If the run succeeded but *diverged* from the published procedure and did so in fewer steps, queue a `review_type = update` card — the agent found a shorter path and a human decides whether it's the new normal

This is how the corpus gets *cheaper* over time, not just larger: the metric that matters is steps-to-completion on repeated tasks, tracked in Section 15.

---

## 12. Processes

### Process 1 — Event-Driven Extraction

The core ongoing loop. Runs on every webhook event from a monitored source.

```
Source event received
  → POST /ingest/event
  → Log to source_events (processed=false)
  → Enqueue for processing

  [RELEVANCE GATE — Groq]
  → Is this content operational decision logic?
  → No  → source_events.outcome = discarded. Stop.
  → Yes → continue

  [CONTEXT EXPANSION]
  → Fetch full surrounding context via source MCP
  → Annotate with source authority tier

  [PASS 1 — DECISION MOMENT IDENTIFICATION — Groq]
  → For threaded sources: extract authoritative decision moments
  → Output: [{message_id, author, timestamp, decision_text, signals}]
  → For non-threaded sources (Notion pages, Google Drive documents, GitHub files): skip to Pass 2

  [PASS 2 — SKILL EXTRACTION — Sonnet]
  → Input: decision moments + authority-annotated context
  → Output: {trigger, base_logic, exceptions, actions, extraction_confidence}

  [EMBEDDING]
  → Call OpenAI text-embedding-3-small on trigger + base_logic

  [BOUNDARY CLASSIFICATION — Groq]
  → pgvector search: top 3 matches, get similarity scores
  → max_similarity > 0.82?
      YES → fast-classifier call: UPDATE | EXCEPTION | DUPLICATE | NEW
      NO  → assume NEW, skip to contradiction check

  UPDATE    → go to CONTRADICTION DETECTOR with diff
  EXCEPTION → append to exceptions_block → go to CONFIDENCE SCORER
  DUPLICATE → source_events.outcome = duplicate. Stop.
  NEW       → go to CONTRADICTION DETECTOR

  [CONTRADICTION DETECTOR]
  → Does proposed content conflict with current published skill?
  → No  → go to CONFIDENCE SCORER
  → Yes → create review_queue row (type=contradiction, source_a, source_b)
           source_events.outcome = contradiction. Stop.

  [CONFIDENCE SCORER]
  → final_confidence = extraction_confidence × authority_multiplier

  [SKILL WRITER]
  → confidence ≥ 0.90 + authority ≥ medium + not sweep
      → publish, version bump, cache invalidate
  → confidence 0.70–0.89 OR authority=low
      → pending_review in review_queue
  → confidence < 0.70
      → draft, logged only

  → source_events.processed = true
  → source_events.outcome = published | queued | draft
```

### Process 2 — Onboarding Sweep

One-time at signup. Populates the brain from historical data.

```
User completes onboarding config
  → POST /ingest/sweep
  → Create sweeps row (status=running)
  → Write monitored sources to source_authority.yaml
  → Set up webhook subscriptions for all selected sources

  [SWEEP WORKER — background job]
  → For each source in authority order (Notion → Google Drive → GitHub → Jira → Slack → Zendesk):
      → Fetch historical items within configured time window
      → For each batch of 10:
          → Run full extraction pipeline (Process 1 steps)
          → EXCEPT: confidence scorer always routes to review_queue
            (auto_publish_during_sweep = false)
          → Rate limit: 10 items/minute/source
      → Update sweeps.progress per source

  [COMPLETION]
  → sweeps.status = completed
  → Notify user: "{n} skills ready for your review"
  → Ongoing webhook monitoring now active
```

### Process 3 — Agent Query (Match Found)

The happy path. Runs on every `query_brain` call when a published skill exists.

```
Agent calls query_brain(situation="...")

  [CACHE CHECK]
  → Hash situation embedding → check Redis
  → Hit → return cached skill. Log to agent_interactions. Stop.
  → Miss → continue

  [EMBEDDING]
  → Call OpenAI text-embedding-3-small on situation string

  [SEMANTIC SEARCH]
  → pgvector cosine search → top 5 published skills
  → max_similarity ≥ 0.70?
      NO  → go to Process 4 (query-driven extraction)
      YES → select best match

  [RESPONSE]
  → Build full skill response (trigger, base_logic, exceptions, actions, metadata)
  → Cache in Redis (5-min TTL)
  → Log to agent_interactions (match_type=semantic)
  → Return to agent
```

### Process 4 — Query-Driven Extraction

Fallback when no published skill matches the agent's query.

```
max_similarity < 0.70 (no match found in Process 3)

  [LIVE SOURCE SEARCH]
  → Use situation string as search query
  → Search Notion designated spaces
  → Search Google Drive designated folders
  → Search Slack monitored channels
  → Search Zendesk resolved tickets
  → Fetch top 5 results across all sources

  [EXTRACTION]
  → Run full extraction pipeline (Process 1 steps)
  → Flag extracted skill: match_type=query_driven

  [IMMEDIATE RESPONSE]
  → Return extracted skill to agent immediately
  → Include flag: "query_driven: true, confidence: low, pending_review: true"

  [STORAGE]
  → Store as draft in review_queue (type=query_driven)
  → Log to agent_interactions (match_type=query_driven)
```

### Process 5 — Boundary Classification

Runs inside Process 1 after Pass 2 extraction completes.

```
New extraction produced

  [SIMILARITY SEARCH]
  → pgvector search: top 3 skills by embedding similarity
    (published only; published + pending_review during sweeps)
  → Record similarity scores

  max_similarity > 0.82?
    NO  → treat as NEW, proceed to Contradiction Detector

    YES → [GROQ BOUNDARY CALL]
          Input: new extraction + matching skill
          Question: UPDATE | EXCEPTION | DUPLICATE | NEW?

          UPDATE    → build diff of base_logic change
                   → proceed to Contradiction Detector with diff
          EXCEPTION → construct exceptions_block row
                   → proceed to Confidence Scorer
                   → (no contradiction check for exceptions)
          DUPLICATE → discard
                   → log source as already covered by skill_id
          NEW       → proceed to Contradiction Detector
```

### Process 6 — Contradiction Resolution

Runs in the review queue when a reviewer opens a contradiction card.

```
Reviewer opens contradiction review card

  Card shows:
  → Source A (higher authority shown first)
  → Source B
  → Current published skill state
  → [Source A correct] [Source B correct] [Write correction]

  [Source A correct]
  → source_a becomes the authoritative basis
  → Skill updates with source_a content
  → changed_by = reviewer_id
  → Version bump, cache invalidate
  → review_queue.status = approved

  [Source B correct]
  → Same as Source A path but with source_b

  [Write correction]
  → Reviewer writes the correct skill content directly in UI
  → changed_by = human_authored
  → confidence = 1.0
  → Auto-publish (human_authored always publishes regardless of confidence routing)
  → Version bump, cache invalidate
  → review_queue.status = human_written
```

### Process 7 — Human Review (Standard)

Runs for update, exception, new, query_driven, and sweep_sourced queue items.

```
Reviewer opens review queue list
  → Sees items ordered: contradiction → update → exception → new → query_driven

Reviewer opens item card
  → Sees proposed change, source context, source authority, confidence

  [APPROVE]
  → Skill updates with proposed content
  → version bump if updating published skill
  → changed_by = reviewer_id
  → cache invalidate
  → review_queue.status = approved

  [REJECT]
  → Proposed update discarded
  → source_events.outcome updated
  → review_queue.status = rejected

  [BULK APPROVE — sweep items only]
  → UI clusters sweep items by similarity > 0.75
  → Reviewing top item → "Apply to similar" → approves entire cluster
```

### Process 8 — Agent-Run Distillation

The self-improving loop. Runs when an agent harness reports a completed run.

```
Agent finishes task
  → POST /runs  (X-API-Key, scope runs:write)

  [INGEST — synchronous, must stay fast]
  → Adapter normalizes harness format → step envelope
  → REDACT secrets/PII, digest oversize args   ← before any write
  → Compute trace_digest, embed task string
  → Insert agent_runs row, enqueue gate job
  → Return 202 {runId}                          ← agent is unblocked here

  [SUCCESS GATE — no LLM]
  → outcome == success?  no override?  error-free tail?  step count in range?
  → No  → eligible = false, ineligible_reason set. Stop. (still counted in metrics)
  → Yes → continue

  [CLUSTERING]
  → cosine ≥ 0.85 vs undistilled runs of same agent_name → join/open cluster
  → cluster size < min_runs_per_cluster AND not human_confirmed?
      → wait for more runs. Stop.
  → threshold met → continue

  [TRAJECTORY COMPRESSION — Groq]
  → Drop dead ends, retries, single-run noise
  → Output: causal spine + recoveries + divergences

  [PROCEDURE EXTRACTION — Sonnet]
  → Input: spine + task + recoveries
  → Output: {trigger, base_logic (ordered steps), exceptions, actions,
             extraction_confidence, open_questions}

  [EMBEDDING]  → same as Process 1

  [BOUNDARY CLASSIFICATION — Groq]  → same as Process 1
  UPDATE against human_authored or high-authority skill
            → CONTRADICTION card (a run does not overturn a human)
  UPDATE    → contradiction detector
  EXCEPTION → append recovery/divergence row
  DUPLICATE → link run to existing skill, bump run_support. Stop.
  NEW       → contradiction detector

  [CONTRADICTION DETECTOR]  → same as Process 1

  [CONFIDENCE SCORER]
  → final_confidence = extraction_confidence × authority_multiplier
      (agent_run: low 0.65, or medium 0.85 when human_confirmed)

  [SKILL WRITER]
  → origin = agent_run ⇒ ALWAYS status = pending_review,
     review_queue row with review_type = procedure. Never auto-publish.

  → agent_runs.skill_id set, distilled_at = now() for every run in cluster
  → raw trace nulled at retention window; digest + skill persist
```

**Backfill path (Feature 29a).** Historical runs enter at the ingest step with `ingest_mode = backfill` and a `sweep_id`, skip nothing else, and differ only in that (a) a missing outcome label is inferred by a Groq call on the trace tail with `ambiguous` excluded, (b) runs older than `lookback_days` are dropped, and (c) clusters are distilled in frequency order under the daily cap rather than as they arrive. Same gate, same compression, same review queue.

**Reinforcement path (no distillation needed).** If the run cited a `query_brain` interaction and followed the returned skill successfully, Feature 34 runs instead of/alongside distillation: confidence bump, `run_support` refresh, and — if the run beat the published procedure on step count — an `update` review card proposing the shorter path.

---

## 13. Onboarding Flow

### Step 1 — Connect Sources

User sees a source connection screen. Each source has a Connect button that initiates OAuth. User can connect any subset of the six sources — doesn't have to be all six.

```
┌─────────────────────────────────────────────────────────┐
│ Connect your company's knowledge sources                │
├─────────────────────────────────────────────────────────┤
│ ○ Slack          [Connect →]                           │
│ ○ Notion         [Connect →]                           │
│ ○ Google Drive   [Connect →]                           │
│ ○ GitHub         [Connect →]                           │
│ ○ Jira           [Connect →]                           │
│ ○ Zendesk        [Connect →]                           │
├─────────────────────────────────────────────────────────┤
│ Already running agents? Import their history:           │
│ ○ LangSmith  ○ Langfuse  ○ Claude Code  ○ Upload NDJSON │
│   → distils procedures your agents already know         │
├─────────────────────────────────────────────────────────┤
│ Connected: 0 of 6                                       │
│                        [Continue with connected →]      │
└─────────────────────────────────────────────────────────┘
```

Agent-run history (Feature 29a) is optional and independent of the six sources — a team with no Slack connected but a year of LangSmith traces still gets a useful brain.

### Step 2 — Configure What to Include

User explicitly selects which channels, spaces, and projects contain operational knowledge. This selection defines both the sweep scope and the ongoing monitoring scope.

```
┌─────────────────────────────────────────────────────────┐
│ Which sources contain policy and process decisions?     │
├─────────────────────────────────────────────────────────┤
│ SLACK — select channels                                 │
│ ☑ #cs-escalations    ☑ #ops-decisions   ☑ #policy     │
│ ☐ #general           ☐ #random          ☐ #dev-chat   │
│ How far back?  [Last 6 months ▼]                       │
├─────────────────────────────────────────────────────────┤
│ NOTION — select spaces                                  │
│ ☑ Operations         ☑ Engineering Runbooks            │
│ ☐ Marketing          ☐ People & Culture                │
├─────────────────────────────────────────────────────────┤
│ GOOGLE DRIVE — select folders                           │
│ ☑ Company Policies   ☑ SOPs & Runbooks                 │
│ ☐ Sales Decks        ☐ Personal                        │
│ How far back?  [Last 6 months ▼]                       │
├─────────────────────────────────────────────────────────┤
│ GITHUB — paths included automatically                   │
│ /docs  /runbooks  /.github/workflows                   │
├─────────────────────────────────────────────────────────┤
│ JIRA — select projects                                  │
│ ☑ OPS   ☑ CS   ☐ MARKETING   ☐ GROWTH                 │
│ ☑ Only include policy/process ticket types             │
├─────────────────────────────────────────────────────────┤
│ ZENDESK                                                 │
│ ☑ Include resolved tickets                             │
│ How far back?  [Last 6 months ▼]                       │
├─────────────────────────────────────────────────────────┤
│                   [Build my brain →]                    │
└─────────────────────────────────────────────────────────┘
```

### Step 3 — Sweep Runs

Progress screen. User can start reviewing the queue while the sweep runs. They do not need to wait for completion.

```
┌─────────────────────────────────────────────────────────┐
│ Building your brain...                                  │
│ You can review skills as they come in — don't wait.    │
├─────────────────────────────────────────────────────────┤
│ ✅ Notion Operations      48 pages     20 queued       │
│ ✅ Notion Runbooks        23 pages      8 queued       │
│ ⏳ Slack #cs-escalations  ████████░░  312 threads      │
│ ⏳ Slack #ops-decisions   ████░░░░░░   89 threads      │
│ ⌛ Jira OPS + CS          waiting...                   │
│ ⌛ Zendesk tickets        waiting...                   │
├─────────────────────────────────────────────────────────┤
│ 28 skills ready for review                              │
│                                                         │
│ [Review queue →]        [I'll check back later]        │
└─────────────────────────────────────────────────────────┘
```

### Important Note on Sweep Design

Skills extracted during the sweep never auto-publish regardless of confidence score. Every sweep-sourced skill requires a human to approve it before it becomes part of the published brain that agents query.

This is intentional. Historical data is less reliable than live events — policies change, Slack threads contain outdated stances, and you do not want an agent running on an automatically populated brain that no human has verified. The sweep gets the brain to a working draft state. Human review gets it to a trusted published state.

### Cold-Start Mitigation

The review requirement creates a cold-start risk: dozens of queued items gated on a non-technical reviewer before any value arrives. Two mitigations:

1. **High-tier fast track** (`sweep.fast_track_high_tier`). Skills extracted from designated high-authority sources (Notion policy pages, Drive policy folders) surface at the top of the queue with a one-click "verify" flow — the reviewer confirms the source is current rather than reviewing full extraction detail. Combined with bulk cluster approval, this gets the first skills published fast without abandoning the human gate.
2. **Time-to-first-value target:** **10 published skills within 1 hour of connecting sources.** This is a product acceptance metric (see Section 15), not just an aspiration — onboarding ordering, fast-track UX, and sweep priority all serve it.
3. **Run-history backfill** (Feature 29a). For teams already running agents, importing existing traces is the fastest path to a populated brain: the runs are pre-labeled by outcome and already describe this company's real tasks. Distilling the top clusters by frequency yields a small set of high-traffic procedure cards — the highest-value review items a new customer can be handed on day one.

---

## 14. API Surface

### FastAPI REST — port 8000

| Method | Path                                 | Description                                            |
| ------ | ------------------------------------ | ------------------------------------------------------ |
| GET    | `/health`                            | Returns `{status, db, redis}`                          |
| GET    | `/skills/search?q={query}&limit={k}` | pgvector semantic search over published skills         |
| GET    | `/skills/{id}`                       | Full skill body                                        |
| GET    | `/skills/{id}/versions`              | Full version history                                   |
| GET    | `/skills/export`                     | Export all published skills as a markdown bundle (zip). The anti-lock-in guarantee — see Section 3, Moat. |
| POST   | `/interactions/{id}/override`        | Report an agent override (Feature 15a feedback loop)   |
| POST   | `/runs`                              | Push an agent run trace (scope `runs:write`, write-only). Returns `202 {runId}` — gating and distillation are async (Feature 29) |
| POST   | `/runs/import`                       | Bulk import historical runs (NDJSON, ≤10k envelopes). Opens a `sweeps` row; JWT `admin` (Feature 29a) |
| GET    | `/runs/import/{sweep_id}/status`     | Backfill progress: imported, gated, clustered, distilled, queued |
| GET    | `/runs`                              | List runs with gate verdict, cluster, and distilled skill (JWT, `editor`+) |
| GET    | `/runs/{id}`                         | Single run: digest, spine, and — for `admin` only — the redacted raw trace while it is still inside the retention window |
| GET    | `/skills/{id}/runs`                  | Runs backing a procedure skill (its evidence trail)    |
| POST   | `/skills`                            | Manual skill create (sets `changed_by=human_authored`) |
| PATCH  | `/skills/{id}`                       | Manual skill edit                                      |
| POST   | `/ingest/event`                      | Webhook receiver                                       |
| POST   | `/ingest/sweep`                      | Trigger onboarding or manual sweep                     |
| GET    | `/ingest/sweep/{sweep_id}/status`    | Sweep progress                                         |
| GET    | `/review`                            | List pending queue items (grouped by type)             |
| GET    | `/review/{id}`                       | Single item with full source context                   |
| POST   | `/review/{id}/approve`               | Approve and publish                                    |
| POST   | `/review/{id}/reject`                | Reject                                                 |
| POST   | `/review/{id}/write`                 | Human writes the correct version                       |
| POST   | `/review/bulk-approve`               | Approve a list of item IDs (sweep bulk review)         |
| GET    | `/connections`                       | List connected sources and their status                |
| DELETE | `/connections/{source}`              | Disconnect a source (revokes active webhook subscriptions) |

### FastMCP server — port 8001

```python
@mcp.tool()
async def query_brain(situation: str) -> dict:
    """
    Query the company brain for the operational skill that matches
    this situation. Call this before executing any company-specific task.

    Returns the skill's trigger, base decision logic, exceptions table,
    available actions, confidence score, and match metadata.

    If no published skill matches, triggers a live extraction from
    connected sources and returns the result flagged as query_driven.
    """


@mcp.tool()
async def report_run(
    task: str,
    outcome: str,
    steps: list[dict],
    interaction_id: str | None = None,
    external_id: str | None = None,
) -> dict:
    """
    Report a completed agent run so the brain can learn from it.
    Call this after finishing a task, whether it succeeded or not.

    Successful runs are distilled into reviewed procedure skills; failed
    runs are kept only as quality signal. Traces are redacted on ingest
    and expire — the durable artifact is the reviewed skill.

    Requires the runs:write scope. Returns {run_id, accepted} immediately;
    gating and distillation happen asynchronously.
    """
```

`report_run` is the ergonomic path for agents already speaking MCP; `POST /runs` is the path for harnesses that ship traces out-of-band (batch exporters, OTel collectors). Both land on the same ingestion service.

---

## 15. Success Metrics

**North star: percentage of agent queries answered by a published, human-reviewed skill** (`match_type = semantic` against a published skill). This measures the whole system — extraction coverage, quality, and currency — in one number.

| Metric | Target | Measures |
| --- | --- | --- |
| Answered-by-published-skill rate | ≥ 70% of `query_brain` calls after week 2 | Coverage + retrieval quality |
| Time to first value | 10 published skills within 1 hour of connecting sources | Onboarding + cold start |
| Reviewer rejection rate | ≤ 25% of surfaced review items | Extraction precision (a free quality signal — track from day one) |
| Contradiction detection recall | ≥ 80% on the synthetic validation set | Safety of auto-publish |
| Relevance gate precision | ≥ 70% of gated-in items survive to queue or publish | Pipeline cost efficiency |
| Agent override rate per skill | ≤ 5% of interactions; alert above | Production skill correctness (Feature 15a) |
| Median review decision time | ≤ 30 seconds per item | Review UX |
| Redis cache hit rate | ≥ 40% of `query_brain` calls | Delivery cost + latency |
| Webhook-to-published latency | ≤ 5 minutes | Living currency promise |
| **Steps-to-completion on repeat tasks** | ≥ 30% reduction after a procedure skill publishes, vs. the median of runs before it | **The self-improving loop working** — this is the number that proves "more usage → cheaper agents" |
| Procedure approval rate | ≥ 60% of procedure cards approved (with or without edits) | Distillation precision — below this, the compression prompt or the cluster threshold is wrong |
| Run distillation cost | ≤ 1 Sonnet call per task cluster, ≤ `max_distillations_per_day` per workspace | Cost discipline of the loop (a per-run distillation would bankrupt it) |
| Backfill yield | ≥ 15 procedure cards from a 90-day trace import | Cold-start value of Feature 29a |
| Agent run success rate | tracked per `agent_name`, no target | Product signal for the customer; ineligible runs are counted, not discarded |

These are product acceptance metrics: phase acceptance criteria below reference them, and instrumentation for each must exist before the phase that depends on it closes.

---

## 16. Phases of Execution

### Phase 1 — Infrastructure

**Scope:** Project structure, Docker environment, database schema, empty service stubs. No business logic.

**Deliverables:**

- `docker-compose.yml` with Postgres (pgvector), Redis, brain-api
- `schema.sql` with all five tables (skills, skill_versions, review_queue, source_events, sweeps, agent_interactions)
- `brain-api/` with FastAPI app, all router stubs returning 501, config, database pool, Redis client
- `mcp_server/server.py` with `query_brain` stub
- `source_authority.yaml` with default config

**Acceptance:**

- `docker compose up` with no errors
- `GET /health` returns `{status: ok, db: true, redis: true}`
- All tables present in Postgres
- pgvector extension installed
- FastMCP reachable on port 8001
- All stubs return HTTP 501

---

### Phase 2 — Source Connections + Onboarding

**Scope:** OAuth connector setup, onboarding configuration UI, sweep worker, source authority annotator, context expanders.

**What "sweep" means in this phase:** ingestion only. The Phase 2 sweep worker fetches historical items and logs them to `source_events` (`processed = false`) in authority order — it does **not** extract, because the extraction pipeline is Phase 3. Phase 3 then runs extraction over the events this phase logged. This makes Phase 2 independently verifiable.

**Connector priority:** Zendesk is the launch wedge (Section 3) and must be treated as first-class, not built last. Remaining connector order: **Zendesk → Jira** (Slack, Notion, GitHub, Google Drive, Gmail already implemented).

**Deliverables:**

- OAuth flows for all six sources, Zendesk prioritized
- Onboarding config UI (channel/space picker + time window)
- Sweep worker (ingestion-only) with priority ordering and rate limiting
- Per-source **API** rate limiting for historical fetches (Notion ~3 rps, Slack tier limits, Zendesk incremental export limits) — distinct from the LLM processing rate limit
- `GET /ingest/sweep/{id}/status` with per-source progress
- Source authority annotator reading `source_authority.yaml`
- Webhook receiver `POST /ingest/event` (logs and enqueues, does not yet extract)
- **Webhook security:** per-source signature verification (Slack signing secret, GitHub HMAC, Notion verification token, Zendesk basic auth/signing), URL-verification handshakes (e.g., Slack `url_verification` challenge echo), and duplicate-delivery dedup keyed on source event ID
- Webhook subscriptions created for monitored sources after onboarding
- **Credential & subscription lifecycle:** transparent OAuth token refresh from `source_connections.refresh_token`; renewal job for expiring subscriptions driven by `webhook_subscriptions.expires_at` (Google Drive channels expire within ~7 days)

**Acceptance:**

- User can complete OAuth for all six sources
- After onboarding config, sweep worker starts and ingests items in authority order; `source_events` populates with `processed = false`
- `GET /ingest/sweep/{id}/status` shows real-time per-source progress
- **Resumability exercised, not assumed:** killing the sweep mid-run and restarting resumes from the last processed item with zero duplicate `source_events` rows
- **Failure isolation:** one source erroring (revoked token, API outage) marks that source failed in `sweeps.progress` and does not halt the other sources
- Webhook subscriptions verified active for all monitored channels
- Unsigned or badly-signed webhook payloads are rejected with 401 and do not create `source_events` rows; redelivered events do not create duplicates
- An expired OAuth token refreshes without user action; a Drive channel nearing expiry is renewed by the renewal job before it lapses

---

### Phase 3 — Extraction Engine

**Scope:** Full extraction pipeline. Relevance gate, two-pass extraction, boundary classifier, contradiction detector, confidence scorer, embedder, skill writer. Extraction runs over the `source_events` rows the Phase 2 sweep ingested (`processed = false`).

**Deliverables:**

- Context expander for each source (moved from Phase 2 in v1.3: expanders are only invoked by the pipeline after the relevance gate, so building them earlier means guessing this phase's interface)
- `services/relevance_gate.py` — Groq classifier
- `services/decision_identifier.py` — Pass 1, Groq thread structurer
- `services/skill_extractor.py` — Pass 2, Sonnet structured extractor
- `services/boundary_classifier.py` — Groq four-way classifier, **searching published + `pending_review` skills during sweeps** (see Feature 7 sweep-scope note — without this, an empty published set classifies every sweep item as NEW and floods the queue with duplicates)
- `services/contradiction_detector.py` — conflict comparison (same sweep-scope rule)
- `services/confidence_scorer.py` — authority-weighted scoring
- `services/embedder.py` — OpenAI embedding wrapper
- `services/skill_writer.py` — write to skills table with routing logic
- **Pipeline failure handling:** transient LLM failures retry with exponential backoff (3 attempts); exhausted retries set `source_events.outcome = failed` (dead-letter) so the sweep never silently drops items; failed events are re-runnable
- **Synthetic validation dataset + eval harness:** the labeled dataset resolving the Section 17 open question, plus a repeatable harness measuring relevance-gate precision, boundary-classification accuracy, and contradiction-detection recall. This is also the regression suite for every future prompt change
- **Cost telemetry:** per-stage LLM cost logged per event; per-sweep cost rollup in `sweeps.progress`
- `POST /review/{id}/approve` and `POST /review/{id}/reject` API endpoints (UI comes in Phase 4; Phase 3 approval happens via API)
- End-to-end processing of sweep items through full pipeline
- `review_queue` populates with sweep-sourced skills

**Acceptance:**

- Running sweep on real or synthetic data produces skills in `review_queue`
- At least 10 published skills after human approval of sweep items **via the approve API** (review UI is Phase 4)
- Duplicate Slack messages about the same policy correctly classified as DUPLICATE — **including during a sweep**, when the matching skill is still `pending_review` rather than published
- At least one contradiction correctly detected and routed to `review_queue` with both sources populated
- Every skill in the `skills` table has a non-null `embedding` vector
- A forced transient LLM failure retries and succeeds; a forced permanent failure lands in `outcome = failed` and is visible in sweep progress, not silently dropped
- Sweep cost rollup reported; pipeline cost per 1,000 sweep items within the budget set at phase start
- **Extraction quality measured, not assumed:** on the synthetic validation set, relevance gate precision ≥ 70%, contradiction detection recall ≥ 80%, reviewer rejection rate ≤ 25% (Section 15). Rejection-rate instrumentation ships with this phase.

---

### Phase 4 — Review System

**Scope:** All review UI card types, bulk sweep review, human-authored override, skill versioning confirmed working.

**Deliverables:**

- Review queue list page (grouped by type, contradictions first)
- Update review card with diff view
- Exception review card
- Contradiction review card (two sources + three action buttons)
- Query-driven review card
- Bulk approve UI for sweep items (cluster by similarity)
- `POST /review/{id}/write` endpoint for human corrections
- Skill versioning verified: every approval creates `skill_versions` row and bumps `skills.version`
- Cache invalidation on every approval

**Acceptance:**

- Reviewer can process a contradiction item in under 30 seconds
- Picking "Source A is correct" updates the skill, creates a version row, invalidates Redis
- Human-written correction sets `changed_by = human_authored`, confidence = 1.0, auto-publishes
- Bulk approve works: approving top item in a cluster with "apply to similar" approves the full cluster
- Rejected items do not appear in published skills or agent responses

---

### Phase 5 — Delivery

**Scope:** `query_brain` fully wired. Query-driven extraction active. Event-driven pipeline live end-to-end. Redis caching confirmed. REST search endpoints complete.

**Deliverables:**

- `query_brain` MCP tool fully implemented (embed → cache check → search → return or fallback)
- Query-driven extraction fallback (Feature 16)
- Event-driven pipeline active: webhook events now trigger full extraction, not just logging
- **Backlog policy:** webhook events logged to `source_events` between Phase 2 activation and this phase (received but never extracted) are replayed through the pipeline on activation, oldest first — or explicitly marked `discarded` by config; they must not remain silently unprocessed
- `GET /skills/search` endpoint live with pgvector results
- Redis cache confirmed working: second identical query served from cache
- `agent_interactions` logging on every `query_brain` call
- Feedback loop (Feature 15a): `POST /interactions/{id}/override` decrements skill confidence and queues review below the auto-publish floor
- Query-driven extraction honors the 15-second latency budget with async fallback
- `GET /skills/export` returns the full published skill corpus as portable markdown

**Acceptance:**

- `query_brain(situation="customer requesting refund past 30 days")` returns a published skill
- Second identical call served from Redis (confirmed via cache hit log)
- Posting a mock Slack policy message to `POST /ingest/event` produces an updated or new skill within 5 minutes
- Query with no matching skill triggers query-driven extraction and returns a result flagged `query_driven: true`
- Query-driven extraction exceeding 15 seconds returns `extraction_queued: true` instead of blocking the agent
- Reporting an override on a published skill drops its confidence and creates a review item once below 0.90

**Implementation status (July 2026):**

Delivered and tested (all on the multi-tenant `app/` tree, RLS-scoped, `X-API-Key`/JWT auth, per-route rate limits):

- `query_brain` MCP tool (`app/mcp/server.py`) — embed → Redis read-cache → pgvector top-5 → semantic match or query-driven fallback. Agents authenticate with the same `X-API-Key` credential as the REST API, gated by the `brain:query` scope, failing closed.
- Delivery REST (`app/modules/skills/`): `GET /skills/search`, `GET /skills/{id}`, `GET /skills/{id}/versions`, `GET /skills/export` (admin-only markdown zip), `POST /interactions/{id}/override` (idempotent Feature 15a loop).
- Read-side cache (`app/pipeline/cache.py::get/set_cached_search`) on the `skills:{workspace_id}:*` keyspace that publish/approve already invalidate.
- `agent_interactions` logging on every `query_brain` and search call.
- Query-driven latency contract (`app/pipeline/query_extraction.py`): 15-second inline budget, async ARQ fallback (`query_extract` job), and interaction logging — all implemented and tested.

**Remaining work — query-driven live-source search (Feature 16).** The one deferred piece is the actual per-source search behind `query_extraction.search_sources`, which currently returns `[]` (an honest no-match) rather than fabricating a skill. It was deferred deliberately: no connector implements search yet (`SourceIntegration` exposes `fetch_since`, not `search`), and shipping untested provider-search clients would violate the security bar. Design and build order:

1. **Add `search(access_token, query, limit) -> list[RawItem]` to `SourceIntegration`** (`app/integrations/base.py`) and implement per provider, decrypting the token from `source_connections` (as `fetch_since` does). Search returns *document references* (ids/urls), not full text:
   - Zendesk — `GET /api/v2/search.json?query=type:ticket <q> status:solved` (build first — the launch wedge)
   - Notion — `POST /v1/search`; Google Drive — `files.list?q=fullText contains '<q>'`
   - Slack — `search.messages` (⚠ needs a **user** token, not a bot token — may require an onboarding scope change; do last)
2. **Fill `search_sources`** to fan out across connected searchable sources concurrently within the 15s budget.
3. **Bridge to the existing pipeline (no expander changes needed):** map the top reference to a `RawEvent`, `JobsRepository.insert_event(...)`, then `run_pipeline(workspace_id, event_id, sweep_sourced=True)` — the per-source expander fetches full context by id exactly as it does for webhooks, and `sweep_sourced=True` forces review routing (query-driven never auto-publishes). Return the resulting skill as `match_type=query_driven`; enqueue remaining references to the `query_extract` job for async completion.
4. **Migration:** `ALTER TYPE review_kind ADD VALUE IF NOT EXISTS 'query_driven'` (own migration, non-transactional) so query-driven reviews carry a distinct kind for the Feature 22 card.

Suggested phasing: ship **Zendesk-only** first (one provider, the wedge, real end-to-end value), then Notion/Drive, then Slack once the user-token scope is settled. Each provider is independently shippable behind the same seam.

---

### Phase 6 — Agent Demo

**Scope:** End-to-end Claude agent demo. Recorded. All acceptance criteria met.

**Deliverables:**

- `agent-demo/demo.py` complete
- Demo runs the refund scenario end-to-end
- Agent resolution is fully traceable to published skill + version

**Acceptance:**

- Demo completes in under 2 minutes
- Agent produces correct resolution (approve_refund + initiate_carrier_claim despite 38-day window) based on the exceptions table entry
- Zero hallucinated policy — every claim in agent output references the returned skill
- Interaction logged to `agent_interactions` with skill_id, query, and match_type

---

### Phase 7 — Agent Run Ingestion (self-improving loop)

**Scope:** successful agent runs become an extraction source. Live push + historical backfill, distilled into reviewed procedure skills. Depends on Phase 5 (delivery + `agent_interactions`) and Phase 4 (review queue); independent of the remaining Feature 16 live-source search.

**Deliverables:**

- `agent_runs` table + migration (`origin`, `run_support` on `skills`; `run_id` on `agent_interactions`; `procedure` review kind)
- `POST /runs` + `report_run` MCP tool, scope `runs:write` (write-only credential), `202` async contract (Feature 29)
- Redaction pass proven by test: secrets, tokens, auth headers, oversize args never reach the database
- Harness adapters: Claude Agent SDK / Claude Code first, then OpenAI-style, LangSmith, OTel
- Success gate + task clustering with the `min_runs_per_cluster` threshold (Feature 30)
- Trajectory compression (Groq) + procedure extractor (Sonnet), rejoining the existing pipeline unchanged (Features 31–32)
- Procedure review card + reject-reason taxonomy (Feature 33)
- Run-outcome reinforcement, including the "agent found a shorter path" update card (Feature 34)
- Run history backfill: `POST /runs/import` + status endpoint, lookback window, inferred-outcome classifier, frequency-ordered cluster draining (Feature 29a)
- Retention job: raw traces nulled at `raw_trace_days`, digest + skill retained

**Acceptance:**

- Pushing 3 successful runs of the same task produces exactly **one** procedure card in the review queue — not three, not zero
- A pushed run with `outcome = failure`, or with a human override on its interaction, produces **no** card and is recorded with an `ineligible_reason`
- No agent-run-derived skill can reach `published` without a human approval, at any confidence — asserted by test, since this is the product's core claim
- A procedure whose steps contradict a `human_authored` skill produces a contradiction card, not an update
- Approving a procedure card publishes a skill whose `base_logic` is materially shorter than the median observed run (steps saved is displayed and non-zero)
- Importing a 90-day trace export produces procedure cards ordered by task frequency, without exceeding `max_distillations_per_day`
- After the retention window, the raw trace is gone but the published skill and its `run_support` provenance remain
- Cross-tenant negative test: a `runs:write` key for workspace A cannot push to, or read runs from, workspace B

---

## 17. Open Questions

| Question                                                                                                                                                                                                                         | Priority | Needed by                                 |
| -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------- | ----------------------------------------- |
| ~~Which data set for Phase 3 validation — pilot customer's real data or a synthetic data set?~~ **Resolved (v1.3):** a synthetic validation dataset + eval harness is a Phase 3 deliverable; pilot data supplements it when available. | ~~High~~ | Resolved                                  |
| Should the review queue send notifications (email, Slack DM) when new items arrive, or is polling the UI sufficient for MVP?                                                                                                     | Medium   | Before Phase 4                            |
| What is the right lookback window default for the sweep? 3 months is safer (less stale data) but 12 months surfaces more institutional knowledge.                                                                                | Medium   | Before Phase 2                            |
| Pricing model: platform fee + per-query, flat monthly, or outcome-based?                                                                                                                                                         | High     | Before any external customer conversation |
| When a human writes a correction (`changed_by = human_authored`), should the original conflicting source URLs still be stored in `source_ids` for traceability, or replaced by the human correction as the sole source?          | Low      | Before Phase 4                            |
| Should the `query_brain` tool accept an optional `entities` dict (customer tier, order value, etc.) that could be used to filter exception conditions? Adds precision but increases integration complexity for agent developers. | Low      | Before Phase 5                            |
| FalkorDB as a lighter-weight future alternative to Neo4j if graph traversal is added post-MVP — worth evaluating now to avoid future migration pain?                                                                             | Low      | Post-MVP                                  |
| Is `min_runs_per_cluster = 3` the right threshold? Too low and we distil luck; too high and rare-but-valuable procedures (incident runbooks, run a handful of times a year) never surface. Possibly frequency-dependent.        | High     | Before Phase 7                            |
| Do customers accept pushing full run traces to us at all? Traces contain file contents and customer records. If redaction-at-ingest is not enough for security review, the fallback is **client-side distillation** — the harness compresses locally and pushes only the spine. Materially different integration; decide before building adapters. | High | Before Phase 7 |
| Who is the reviewer for a procedure card? Policy cards go to a CS/ops lead; a "rotate the API key" procedure needs an engineer. Does the review queue need per-card routing by skill domain?                                    | Medium   | Before Phase 7                            |
| Should a procedure skill record *which model* produced the run? A trace from a frontier model may encode steps a smaller model can't follow, making the procedure unusable for the agent that queries it.                       | Medium   | Before Phase 7                            |
| Pricing implication of the loop: run ingestion costs us LLM spend per cluster but makes the customer's agents cheaper. Is backfill a paid onboarding accelerator, or free because it drives the moat?                          | Medium   | With pricing model                        |
