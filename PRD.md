# Company Brain — Product Specification

**Version:** 1.0  
**Status:** Active  
**Last updated:** May 2026

---

## Table of Contents

1. [Overview](#1-overview)
2. [Problem Statement](#2-problem-statement)
3. [What We Are Not Building](#3-what-we-are-not-building)
4. [Users](#4-users)
5. [Architecture](#5-architecture)
6. [Tech Stack](#6-tech-stack)
7. [Skill Format](#7-skill-format)
8. [Data Model](#8-data-model)
9. [Source Authority Config](#9-source-authority-config)
10. [Features](#10-features)
11. [Processes](#11-processes)
12. [Onboarding Flow](#12-onboarding-flow)
13. [API Surface](#13-api-surface)
14. [Phases of Execution](#14-phases-of-execution)
15. [Open Questions](#15-open-questions)

---

## 1. Overview

Company Brain is the missing layer between raw company data and reliable AI automation.

Every company runs on operational knowledge that exists nowhere a machine can read — in Slack threads, Notion pages, Zendesk ticket resolutions, GitHub pull request discussions, and people's heads. AI agents fail on company-specific tasks not because the models are weak but because this knowledge is inaccessible to them.

Company Brain solves this by connecting to every source that knowledge lives in, extracting it into structured executable skills, keeping those skills current as the company evolves, and serving them to any AI agent through a standard interface.

The output is not a search result or a document summary. It is an executable skill: a structured, versioned, human-reviewed description of how the company handles a specific situation, complete with trigger conditions, decision logic, exceptions, and the actions an agent can take.

**One-line pitch:** Every AI agent in the world will eventually hit the wall of not knowing company-specific logic. Company Brain is the infrastructure that solves that — once, permanently, for every company.

---

## 2. Problem Statement

Companies deploying AI agents hit the same failure mode consistently: the agent handles generic tasks well but collapses on company-specific edge cases. Pricing exceptions, escalation rules, return policy thresholds, incident runbooks — this logic exists nowhere a machine can reliably read.

It lives in:

- Slack threads from 14 months ago where a policy decision was made in message 47 of a 50-message chain
- Notion pages that describe how things worked before the last three policy revisions
- Zendesk ticket resolutions that collectively encode how edge cases are actually handled, but are buried in thousands of tickets
- GitHub pull request review comments where engineering exceptions and rollback decisions were debated
- Jira tickets whose comment history captures how incidents are actually routed
- The head of the CS lead who handles every exception by pattern recognition built over years

AI agents have no authoritative source for any of this. They hallucinate policy, apply stale rules, or fail on edge cases in ways that damage customer trust and require expensive human correction.

The right fix is not better prompting or larger context windows. It is a dedicated infrastructure layer that extracts this knowledge, structures it, keeps it current, and serves it to agents on demand. That is Company Brain.

---

## 3. What We Are Not Building

These decisions reflect deliberate choices to keep the MVP shippable and the product honest.

| Not building                                | Reason                                                                                                                                                          |
| ------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| A chatbot over documents                    | That is a solved problem. Company Brain produces executable skills, not search results.                                                                         |
| A knowledge graph (Neo4j + Graphiti)        | Boundary classification and override detection via vector similarity + LLM calls handles what the graph was supposed to do, without the operational complexity. |
| Airbyte batch ingestion                     | Replaced entirely by MCP connectors + webhooks. Sources are queried directly.                                                                                   |
| PM4Py process mining                        | A Sonnet call over resolved Zendesk tickets produces equivalent output for MVP purposes.                                                                        |
| ExIde two-stage extraction                  | Replaced by a cleaner two-pass extraction design with better separation of concerns.                                                                            |
| Multi-tenant PII redaction                  | Required before external enterprise customers. Not for internal prototype.                                                                                      |
| Fine-tuned content classifier               | Claude Haiku via prompt handles classification at MVP scale.                                                                                                    |
| Salesforce, HubSpot, Gmail, Gong connectors | Post-MVP. Five sources are sufficient to prove the extraction pipeline.                                                                                         |

---

## 4. Users

### Primary — AI engineers deploying support or ops agents

These are the people who keep hitting the "agent doesn't know our process" wall. At a 50–500 person B2B SaaS company, this is typically one person: the engineer who owns the agent stack. They have a tooling budget, they can self-serve an API integration, and they do not need procurement approval.

**Pain:** Writing and maintaining bespoke prompt context for every policy edge case. When policy changes, they have to find every prompt it touches and update each manually. When an edge case breaks the agent, they have to diagnose it manually and patch it by hand.

**How they use Company Brain:** Connect sources during onboarding. Point MCP server at their agent framework. The agent calls `query_brain(situation)` before each task. Brain returns the matched skill with decision logic and actions. Done.

### Secondary — CS or ops team leads who manage the review queue

When the extraction engine produces a low-confidence skill or detects a contradiction between sources, it goes to the review queue instead of auto-publishing. This person reviews proposed changes, sees the source context, and approves, rejects, or corrects. They are not technical.

**How they use Company Brain:** Simple review UI. See the proposed change alongside the source it came from. Make a decision in under 30 seconds per item. The UI is designed so they never need to understand the extraction system — just the content.

---

## 5. Architecture

Company Brain is organized into three layers. The onboarding sweep populates the brain at signup. The event-driven pipeline keeps it current thereafter. The delivery layer serves skills to agents.

```
┌─────────────────────────────────────────────────────────────────┐
│  SOURCES                                                        │
│  Slack · Notion · GitHub · Jira · Zendesk                       │
│  Connected via MCP clients + webhook subscriptions              │
└───────────────────────────┬─────────────────────────────────────┘
                            │
              ┌─────────────┴──────────────┐
              │ ONBOARDING SWEEP           │ EVENT-DRIVEN
              │ (one-time, at signup)      │ (ongoing, webhook)
              └─────────────┬──────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────────┐
│  EXTRACTION ENGINE                                              │
│  Relevance gate → Context expansion → Two-pass extraction       │
│  → Boundary classification → Contradiction detection           │
│  → Confidence scoring → Skill writing + embedding              │
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
                     AI AGENTS
```

### Living Currency

Every source connected during onboarding also has a webhook subscription created for ongoing monitoring. When a relevant event occurs in a monitored Slack channel, Notion space, or Jira project, the extraction engine re-processes only the affected content. Updated skills publish within five minutes of the source event.

### Hybrid Retrieval

For every agent query:

1. Embed the situation string
2. Check Redis cache — hit: return immediately
3. pgvector cosine similarity search over published skill embeddings → top 5 candidates
4. If max similarity ≥ 0.70: return best match
5. If max similarity < 0.70: trigger query-driven extraction from live sources

---

## 6. Tech Stack

| Component          | Technology                    | Notes                                                                    |
| ------------------ | ----------------------------- | ------------------------------------------------------------------------ |
| API server         | FastAPI (Python)              | REST endpoints port 8000. Extraction engine lives here.                  |
| MCP server         | FastMCP                       | `query_brain` tool on port 8001. SSE transport.                          |
| Database           | PostgreSQL 16 + pgvector      | Skills registry, versioning, review queue, event log.                    |
| Vector index       | pgvector IVFFlat              | Cosine similarity on 1536-dim skill embeddings.                          |
| Cache              | Redis 7                       | 5-min TTL on search results. Invalidated on publish/update.              |
| Content classifier | Claude Haiku                  | Relevance gate, decision moment identification, boundary classification. |
| Skill extractor    | Claude Sonnet                 | Two-pass extraction from authority-annotated context.                    |
| Embeddings         | OpenAI text-embedding-3-small | 1536 dimensions. Skills table + agent query embedding.                   |
| Container runtime  | Docker + Docker Compose       | All services. Single compose file.                                       |
| Review UI          | FastAPI + Jinja2              | Minimal HTML. No frontend framework needed for MVP.                      |
| Agent demo         | Anthropic Python SDK          | Claude calls FastMCP directly. No orchestration framework.               |

---

## 7. Skill Format

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

---

## 8. Data Model

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
  -- update | exception | contradiction | new | query_driven | sweep_sourced
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
  -- slack | notion | github | jira | zendesk
  event_type   VARCHAR(50),
  -- message | page_update | pr_merged | issue_closed | ticket_resolved | etc
  source_id    VARCHAR(255),
  payload      JSONB,
  processed    BOOLEAN DEFAULT FALSE,
  skill_id     UUID,
  -- set after extraction if a skill was created or updated
  outcome      VARCHAR(30),
  -- published | queued | discarded | duplicate | contradiction
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

Stub for episodic feedback. Logged on every `query_brain` call. Processed in v2.

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
  created_at         TIMESTAMP DEFAULT NOW()
);
```

---

## 9. Source Authority Config

Loaded from `source_authority.yaml` at startup. Not stored in the database. Defines which sources are authoritative, which are monitored for ongoing events, and what the routing thresholds are.

```yaml
# source_authority.yaml

tiers:
  high:
    weight: 1.0
    sources:
      - type: notion
        signals: [designated_policy_page, owner_edited]
      - type: github
        signals: [path_prefix=/docs, path_prefix=/runbooks]
      - type: jira
        signals: [ticket_type=policy, status=done]

  medium:
    weight: 0.7
    sources:
      - type: slack
        signals:
          [
            channel=policy,
            channel=ops-decisions,
            channel=cs-escalations,
            channel=engineering-decisions,
          ]
      - type: zendesk
        signals: [tag=policy-exception, status=solved]

  low:
    weight: 0.4
    sources:
      - type: slack
        signals: []
      - type: github
        signals: [content_type=comment]
      - type: jira
        signals: [content_type=comment]

routing:
  auto_publish_confidence: 0.90
  auto_publish_authority_floor: medium
  review_queue_confidence_floor: 0.70
  # below 0.70 → draft, not surfaced to reviewers

monitored:
  slack_channels: [] # populated from onboarding config
  notion_spaces: [] # populated from onboarding config
  jira_projects: [] # populated from onboarding config
  github_paths: [/docs, /runbooks, /.github]
  zendesk_tags: [policy-exception, escalation-approved, exception-granted]

sweep:
  processing_order:
    - notion
    - github
    - jira
    - slack
    - zendesk
  rate_per_minute: 10 # items processed per source per minute
  semaphore_limit: 5 # concurrent LLM calls
  auto_publish_during_sweep: false
  # all sweep extractions go to review_queue regardless of confidence
```

---

## 10. Features

### Layer 1 — Source Connections

**Feature 1 — Webhook receiver**
`POST /ingest/event` receives incoming events from all connected sources. Validates the payload, logs to `source_events`, and enqueues for extraction processing.

Supported event types per source:

| Source  | Event types                                                                   |
| ------- | ----------------------------------------------------------------------------- |
| Slack   | `message` in monitored channels, `message_changed` (edits), `pin_added`       |
| Notion  | `page.updated`, `page.created` in monitored spaces                            |
| GitHub  | `pull_request.closed` (merged), `issue.closed`, `push` to monitored paths     |
| Jira    | `issue.updated` (status transitions), `comment.created` on monitored projects |
| Zendesk | `ticket.updated` (solved), `ticket.tagged` with monitored tags                |

**Feature 2 — Context expanders**
One expander per source. Each knows how to fetch full surrounding context for an event, not just the triggering message. Called during extraction after the relevance gate passes.

| Source  | What the expander fetches                                            |
| ------- | -------------------------------------------------------------------- |
| Slack   | Full thread from the message ID, including all replies and reactions |
| Notion  | Full page body + parent page title + linked page titles and excerpts |
| GitHub  | PR description + all review comments + body of linked issues         |
| Jira    | Ticket body + all comments + linked tickets + transition log         |
| Zendesk | Ticket + all comments + tags + resolution note                       |

**Feature 3 — Source authority annotator**
Reads `source_authority.yaml`, determines the tier of an incoming event or sweep item based on source type and signals, and annotates the context before passing it to extraction. The tier is passed explicitly to the extraction prompt so the LLM knows how to weight conflicting information.

---

### Layer 2 — Extraction Engine

**Feature 4 — Relevance gate** (Claude Haiku)
Single LLM call. Binary question: does this content contain operational decision logic, a policy rule, a process instruction, or an exception to an existing rule? Yes → proceed. No → mark `source_events.outcome = discarded`. This is the cheapest call in the pipeline and filters most noise before any expensive processing.

**Feature 5 — Pass 1: Decision moment identifier** (Claude Haiku)
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

**Feature 7 — Boundary classifier** (Claude Haiku)
After extraction, runs a pgvector cosine search over published skills (top 3 matches). If max similarity > 0.82, sends the new extraction alongside the matching skill to Haiku with a four-way classification question:

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
Runs when the boundary classifier returns UPDATE or NEW. Compares the proposed change against the current published skill. If the proposed content conflicts with the existing content on the same condition:

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
Calls OpenAI `text-embedding-3-small` on the skill's `trigger` + `base_logic` text. Stores the resulting 1536-dimension vector in `skills.embedding`. Called after skill_writer completes. Also called at query time to embed the agent's situation string for semantic search.

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

1. Use the situation string as a search query against source MCPs (Notion, Slack designated channels, Zendesk)
2. Fetch top results from connected sources
3. Run full extraction pipeline on combined results (same as event-driven)
4. Return the extracted skill to the agent immediately, flagged as `match_type: query_driven`
5. Store as draft in `review_queue` with `review_type: query_driven` for human promotion to published
6. If the agent interaction is later marked as successful (no override, no correction), confidence bumps on next review

**Feature 17 — FastAPI REST API**

See Section 13 for full endpoint reference.

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
OAuth connection flow for each source. Slack, Notion, GitHub, Jira, and Zendesk each have their own OAuth screen. Connection credentials are stored and used for both the onboarding sweep and ongoing webhook subscriptions.

**Feature 25 — Onboarding configuration UI**
Channel, space, and project picker with time window selector. The user explicitly chooses which sources contain operational knowledge. This configuration:

1. Drives the onboarding sweep (what to ingest)
2. Sets up webhook subscriptions (what to monitor going forward)
3. Populates the `monitored` section of `source_authority.yaml`

**Feature 26 — Sweep worker**
Background job that processes historical content in authority-priority order with rate limiting. Resumable: if the job fails, it picks up from the last processed item using the `source_events.sweep_id` field. Progress is tracked per source in the `sweeps` table.

Processing order: Notion → GitHub → Jira → Slack → Zendesk

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

## 11. Processes

### Process 1 — Event-Driven Extraction

The core ongoing loop. Runs on every webhook event from a monitored source.

```
Source event received
  → POST /ingest/event
  → Log to source_events (processed=false)
  → Enqueue for processing

  [RELEVANCE GATE — Haiku]
  → Is this content operational decision logic?
  → No  → source_events.outcome = discarded. Stop.
  → Yes → continue

  [CONTEXT EXPANSION]
  → Fetch full surrounding context via source MCP
  → Annotate with source authority tier

  [PASS 1 — DECISION MOMENT IDENTIFICATION — Haiku]
  → For threaded sources: extract authoritative decision moments
  → Output: [{message_id, author, timestamp, decision_text, signals}]
  → For non-threaded sources (Notion pages, GitHub files): skip to Pass 2

  [PASS 2 — SKILL EXTRACTION — Sonnet]
  → Input: decision moments + authority-annotated context
  → Output: {trigger, base_logic, exceptions, actions, extraction_confidence}

  [EMBEDDING]
  → Call OpenAI text-embedding-3-small on trigger + base_logic

  [BOUNDARY CLASSIFICATION — Haiku]
  → pgvector search: top 3 matches, get similarity scores
  → max_similarity > 0.82?
      YES → Haiku call: UPDATE | EXCEPTION | DUPLICATE | NEW
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
  → For each source in authority order (Notion → GitHub → Jira → Slack → Zendesk):
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
  → pgvector search: top 3 published skills by embedding similarity
  → Record similarity scores

  max_similarity > 0.82?
    NO  → treat as NEW, proceed to Contradiction Detector

    YES → [HAIKU BOUNDARY CALL]
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

---

## 12. Onboarding Flow

### Step 1 — Connect Sources

User sees a source connection screen. Each source has a Connect button that initiates OAuth. User can connect any subset of the five sources — doesn't have to be all five.

```
┌─────────────────────────────────────────────────────────┐
│ Connect your company's knowledge sources                │
├─────────────────────────────────────────────────────────┤
│ ○ Slack          [Connect →]                           │
│ ○ Notion         [Connect →]                           │
│ ○ GitHub         [Connect →]                           │
│ ○ Jira           [Connect →]                           │
│ ○ Zendesk        [Connect →]                           │
├─────────────────────────────────────────────────────────┤
│ Connected: 0 of 5                                       │
│                        [Continue with connected →]      │
└─────────────────────────────────────────────────────────┘
```

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

---

## 13. API Surface

### FastAPI REST — port 8000

| Method | Path                                 | Description                                            |
| ------ | ------------------------------------ | ------------------------------------------------------ |
| GET    | `/health`                            | Returns `{status, db, redis}`                          |
| GET    | `/skills/search?q={query}&limit={k}` | pgvector semantic search over published skills         |
| GET    | `/skills/{id}`                       | Full skill body                                        |
| GET    | `/skills/{id}/versions`              | Full version history                                   |
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
```

---

## 14. Phases of Execution

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

**Deliverables:**

- OAuth flows for all five sources
- Onboarding config UI (channel/space picker + time window)
- Sweep worker with priority ordering and rate limiting
- `GET /ingest/sweep/{id}/status` with per-source progress
- Context expander for each source
- Source authority annotator reading `source_authority.yaml`
- Webhook receiver `POST /ingest/event` (logs and enqueues, does not yet extract)
- Webhook subscriptions created for monitored sources after onboarding

**Acceptance:**

- User can complete OAuth for all five sources
- After onboarding config, sweep worker starts and processes items in authority order
- `GET /ingest/sweep/{id}/status` shows real-time per-source progress
- `source_events` table populates during sweep
- Webhook subscriptions verified active for all monitored channels

---

### Phase 3 — Extraction Engine

**Scope:** Full extraction pipeline. Relevance gate, two-pass extraction, boundary classifier, contradiction detector, confidence scorer, embedder, skill writer.

**Deliverables:**

- `services/relevance_gate.py` — Haiku classifier
- `services/decision_identifier.py` — Pass 1, Haiku thread structurer
- `services/skill_extractor.py` — Pass 2, Sonnet structured extractor
- `services/boundary_classifier.py` — Haiku four-way classifier
- `services/contradiction_detector.py` — conflict comparison
- `services/confidence_scorer.py` — authority-weighted scoring
- `services/embedder.py` — OpenAI embedding wrapper
- `services/skill_writer.py` — write to skills table with routing logic
- End-to-end processing of sweep items through full pipeline
- `review_queue` populates with sweep-sourced skills

**Acceptance:**

- Running sweep on real or synthetic data produces skills in `review_queue`
- At least 10 published skills after human approval of sweep items
- Duplicate Slack messages about the same policy correctly classified as DUPLICATE
- At least one contradiction correctly detected and routed to `review_queue` with both sources populated
- Every skill in the `skills` table has a non-null `embedding` vector

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
- `GET /skills/search` endpoint live with pgvector results
- Redis cache confirmed working: second identical query served from cache
- `agent_interactions` logging on every `query_brain` call

**Acceptance:**

- `query_brain(situation="customer requesting refund past 30 days")` returns a published skill
- Second identical call served from Redis (confirmed via cache hit log)
- Posting a mock Slack policy message to `POST /ingest/event` produces an updated or new skill within 5 minutes
- Query with no matching skill triggers query-driven extraction and returns a result flagged `query_driven: true`

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

## 15. Open Questions

| Question                                                                                                                                                                                                                         | Priority | Needed by                                 |
| -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------- | ----------------------------------------- |
| Which data set for Phase 3 validation — pilot customer's real data or a synthetic data set designed to stress-test contradiction detection and boundary classification?                                                          | High     | Before Phase 3                            |
| Should the review queue send notifications (email, Slack DM) when new items arrive, or is polling the UI sufficient for MVP?                                                                                                     | Medium   | Before Phase 4                            |
| What is the right lookback window default for the sweep? 3 months is safer (less stale data) but 12 months surfaces more institutional knowledge.                                                                                | Medium   | Before Phase 2                            |
| Pricing model: platform fee + per-query, flat monthly, or outcome-based?                                                                                                                                                         | High     | Before any external customer conversation |
| When a human writes a correction (`changed_by = human_authored`), should the original conflicting source URLs still be stored in `source_ids` for traceability, or replaced by the human correction as the sole source?          | Low      | Before Phase 4                            |
| Should the `query_brain` tool accept an optional `entities` dict (customer tier, order value, etc.) that could be used to filter exception conditions? Adds precision but increases integration complexity for agent developers. | Low      | Before Phase 5                            |
| FalkorDB as a lighter-weight future alternative to Neo4j if graph traversal is added post-MVP — worth evaluating now to avoid future migration pain?                                                                             | Low      | Post-MVP                                  |
