# Brain Chat (RAG) — implementation plan

Plan for **BACKEND_ASKS §7** — the "Ask the brain" dashboard chat. Covers the RAG
architecture, a readiness gate that keeps chat **disabled until skills are
ingested**, and ingestion of **all skill versions (current + historical)**.

Backend-only doc. Everything is judged by API surface, per `CLAUDE.md`.

---

## TL;DR — do we need to build a RAG?

**No — not from scratch. The retrieval half already exists and is live.** The
honest gap is generation + a dashboard-reachable endpoint + a readiness gate +
embedding the version history. Concretely:

| RAG stage | Status in this repo | Where |
|-----------|---------------------|-------|
| **Embed** | ✅ done | `app/pipeline/embedder.py` — OpenAI `text-embedding-3-small`, 1536-dim |
| **Store (vector DB)** | ✅ done | `skills.embedding vector(1536)` + `skills_embedding_hnsw` HNSW cosine index, **per-workspace, RLS-scoped**, kept fresh by the pipeline at write time |
| **Retrieve** | ✅ done | `PipelineRepository.similar_skills()` — HNSW top-K cosine, `1 - (embedding <=> vec)` |
| **Rank / threshold** | ✅ done | `SkillsService.query()` — top-5, match floor `0.70`, Redis 5-min read-cache |
| **Generate (synthesize an answer)** | ❌ **missing** | `query()` returns **one skill's raw fields**, not a cited, conversational answer |
| **Dashboard reach (JWT)** | ❌ **missing** | only the MCP tool (port 8001, `X-API-Key`) exposes it; a browser JWT user has nothing to call |
| **Readiness gate** | ❌ **missing** | nothing disables chat until the index is populated |
| **Version history in the index** | ❌ **missing** | `skill_versions` has **no embedding column** — "previous" versions aren't retrievable |

> **Do not stand up a second vector DB (Pinecone/Chroma/pgvector-elsewhere).**
> That would throw away three things this repo already gives us for free:
> Postgres **RLS tenant isolation**, **publish-time freshness** (no re-index step),
> and the **Redis read-cache + invalidation-on-publish**. Keep retrieval in
> Postgres/pgvector. "Building the RAG" here = adding the **G**, exposing it to
> the dashboard, gating it, widening the corpus to include versions, and capturing
> each skill's full **evidence graph** (Phase 3) so it can answer any question about
> a skill and its lineage.

---

## Scope: a provenance-complete Skills-RAG (store everything that fed a skill)

The chosen scope. Three things get called "RAG" here; we're building the middle one:

- **Bare Skills-RAG**: retrieval over just the human-reviewed **skills** — the
  canonical decisions (trigger + base logic + exceptions). Answers "what's our policy"
  but not "who said we needed it / who approved it / what doc it came from."
- **Provenance-complete Skills-RAG** (✅ chosen — the middle path): retrieval over
  skills **plus everything that fed each skill** — the source message/document, its
  author, the trigger, the exceptions, and the human reviewer. Answers any question
  about a skill *and its lineage*, stays governed, and is bounded by **skill count
  (hundreds), not total ingested volume**.
- **Corpus-RAG** ("index everything we ingest"): retrieval over all raw content.
  Open-ended but ungoverned, with a much larger privacy + cost surface. **Not what
  we're building** — we store only what became part of a skill.

**What we keep today vs. the gap.** We already store *fragments* of a skill's lineage:
`reviews.evidence_quote` + `evidence_author` (the deciding message + who said it),
`reviews.resolved_by` (the reviewer), `source_events.payload` (the raw triggering
item, keyed by event), plus the skill's own trigger/exceptions. The gap is that this
evidence is (a) **partial** — only a 500-char quote survives; the fuller context the
expander fetched is discarded (`orchestrator._expand`, `orchestrator.py:88`);
(b) **unresolved** — Slack/Zendesk/Notion authors are raw ids (decision F); and
(c) **not embedded/retrievable** — it's scattered across audit rows, not indexed as
part of the skill's searchable knowledge. So the work is to **capture each skill's
evidence completely at write-time, resolve the people, link it to the skill, and embed
it** — *not* to index all ingested chatter.

**Existing scaffolding (the schema already anticipates chat + ingest):**
- `brain_builds` — a `queued→running→completed` **ingestion/build job** with
  `progress`, `current_step`, `time_range` (`30d|90d|6mo|all`), `source_ids`,
  `counts`. **This is the "ingest at runtime, show progress, disable chat until
  done" concept** — Phase 0 readiness should hang off a completed `brain_builds`
  row rather than a bare `COUNT`.
- `brain_conversations` + `brain_messages` — chat persistence. `brain_messages`
  already carries `role`, `content`, `confidence` (0–100), and `sources` JSONB
  `[{provider, label, sourceItemId, url, excerpt}]` — **the exact citation
  contract**. Brain-chat persists turns here; it does **not** invent new tables.

**Direction (Open decision G):** build the **provenance-complete Skills-RAG** — the
brain answers from skills and their **evidence graph** (source message + author +
document + reviewer + trigger + exceptions), all governed and skill-linked. No skill
match → an honest "no reviewed skill covers that yet," never a guess from raw chatter.
The general Corpus-RAG is explicitly out of scope.

---

## What "ingest at runtime, disable chat before that" means here

The user's mental model ("load docs → embed → store → then chat can query") is the
standard RAG loop — but in this codebase **current skills are already ingested
continuously**: every approve/publish embeds the skill and writes its vector. There
is no batch load step for current skills.

So "ingest at runtime" resolves to exactly two real pieces of work:

1. **Backfill** — embed anything not yet embedded: (a) any pre-existing published
   skill missing an `embedding`, and (b) **all historical `skill_versions`** (new
   schema — see Phase 2). This is a one-shot, **idempotent** job that can also run
   at boot / on demand.
2. **Readiness gate** — chat stays **disabled** for a workspace until its index
   passes a readiness check, and a global kill-switch can disable it everywhere.

"Ingest all versioned skills, previous + new" = Phase 2 (embed the version history
into the chat corpus). "Disable chat before that" = the readiness gate (Phase 0).

---

## Target architecture

```
                    POST /api/v1/brain/query   (JWT dashboard  OR  X-API-Key agent)
                                │
                                ▼
                     ┌──────────────────────┐
                     │  brain readiness gate │  disabled/not-ready → 409 brain_not_ready
                     └──────────┬───────────┘
                                │ ready
                                ▼
          (1) embed question ───────────────────►  OpenAI embeddings   [network, OUTSIDE txn]
                                │
                                ▼
          (2) retrieve  ── run_in_tenant (RLS, brain_app pool) ─────────────────┐
              GOVERNED index: skills + brain_chunks(kind IN                     │  txn A
              ('skill_version','evidence')) top-K + full bodies + provenance    │
                                │  ◄── commit                                    ┘
                                ▼
          (3) synthesize ──────────────────────►  Claude (sonnet_json)  [network, OUTSIDE txn]
              trust='skill' (reviewed rule) | 'evidence' (cited source) | 'none'
              grounded answer + citations + confidence, or honest "no match"
                                │
                                ▼
          (4) persist ── run_in_tenant ── agent_interactions + brain_messages ───┐  txn B
                                │  ◄── commit;  cache grounded answers (5-min TTL) ┘
                                ▼
             { answer, trust, sources, confidence, skillIds, interactionId }
```

The two-phase txn split (retrieve in txn A, **synthesize outside any txn**, log in
txn B) is the non-negotiable "no network I/O inside an open transaction" rule
(`CLAUDE.md` #7; pattern: `app/modules/reviews/service.py::approve`). An LLM call
must never pin a pooled connection.

---

## Recommended approach: provenance-complete, shipped in phases

**Decision G = provenance-complete Skills-RAG** (chosen): the brain answers from
skills **and each skill's evidence graph** — all governed, skill-linked. Phase 1
unblocks the whole frontend feature fast with near-zero schema risk by reusing
`skills.embedding`. Phase 2 adds skill-version history. Phase 3 captures + indexes
each skill's evidence (source message, author, document, reviewer). The readiness
gate, synthesis, and persistence layers are written once in Phase 1 and reused.

| Phase | Delivers | Trust tier | Schema |
|-------|----------|---------------------|--------|
| **0** | Readiness gate (via `brain_builds`) + kill-switch + `GET /brain/status` | — | reuse `brain_builds` |
| **1** | Synthesis + `POST /brain/query` + chat persistence — **unblocks BACKEND_ASKS §7** | **governed:** current skills (`skills.embedding`) | reuse `brain_conversations`/`brain_messages` |
| **2** | Versioned skill ingestion — "previous + new" | **governed:** + historical skill versions | `brain_chunks` (`kind='skill_version'`) |
| **3** | **Skill Evidence Graph** — capture + index everything that fed each skill | **governed:** skill + evidence (source msg, author, doc, reviewer) | `brain_chunks` (`kind='evidence'`) + write-time evidence capture |

---

## Phase 0 — Readiness gate (disable chat until ingested)

**Goal:** the FE can render a disabled chat, and a query while disabled fails
typed, never silently wrong.

- **Global kill-switch:** add `brain_chat_enabled: bool = True` to
  `app/config/settings.py`. Ops can hard-disable everywhere.
- **Per-workspace readiness (via `brain_builds`):** the schema already has a
  `brain_builds` job table (`queued→running→completed`, `progress`, `current_step`,
  `counts`) — the intended home for "ingest at runtime, show progress, disable until
  done." A workspace is *ready* when its latest `brain_builds` row is `completed`
  **and** it has ≥1 embedded published skill. Phase 1 can bootstrap readiness from a
  bare `COUNT(*)` on `skills`; Phases 2–3 drive it off the `brain_builds` lifecycle
  (and `GET /brain/status` returns `progress`/`current_step` so the FE shows a build
  bar instead of a dead chat).
- **New typed error:** `BrainNotReadyError` (`app/shared/errors/`) → HTTP **409**
  with `code: "brain_not_ready"` and a `reason` (`disabled` | `no_skills` |
  `indexing`). Never a 500, never a fabricated answer.
- **Status endpoint** (so the FE greys out chat *without* a failed query):

  ```
  GET /api/v1/brain/status        (require_brain_access("brain:query"))
  → { "enabled": true, "ready": true, "skillsIndexed": 37, "reason": null }
  ```

`POST /brain/query` calls the gate first; disabled/not-ready short-circuits before
any embedding or LLM spend.

---

## Phase 1 — Synthesis + REST endpoint (the MVP that unblocks the FE)

### New module: `app/modules/brain/`

Standard module shape (`router → service → repository`, `CLAUDE.md` #8). Retrieval
is **reused**, not rebuilt.

```
app/modules/brain/
  router.py       # POST /brain/query, GET /brain/status  (paths + deps only)
  service.py      # BrainService: gate → embed → retrieve → synthesize → log
  synthesizer.py  # LLM answer generation (grounding + citations + confidence)
  schemas.py      # BrainQueryRequest / BrainQueryResponse (Camel(Request)Model)
  repository.py   # only if Phase-1-specific SQL appears; else reuse existing repos
```

`BrainService` **composes existing pieces** — `embedder.embed_text`,
`PipelineRepository.similar_skills`, `SkillsRepository.get` / `insert_interaction`,
`cache.*`. It does **not** duplicate vector SQL.

### Endpoint contract (matches BACKEND_ASKS §7 exactly)

```
POST /api/v1/brain/query
  auth:  require_brain_access("brain:query")   # admits dashboard JWT (role≥viewer) AND agent API-key (scope)
  rate:  @limiter.limit(BRAIN_LIMIT, key_func=workspace_key)   # 120/min, already defined
  body:  { "question": "What is our refund window for premium customers?",
           "conversationId": "cnv_…" }   // optional; omitted → a new conversation is created
  200 →  {
           "answer": "Premium customers have a 45-day refund window…",
           "trust": "skill",          // skill (reviewed rule) | evidence (cited source) | none  — all governed
           "sources": [ { "provider": "notion", "location": "Policy Library", "skillId": "…" } ],
           "confidence": 96,          // 0–100
           "skillIds": ["…"],
           "matchType": "semantic",   // semantic | evidence | no_match  (query_driven via Feature 16)
           "conversationId": "cnv_…", "messageId": "msg_…",  // persisted in brain_messages
           "interactionId": "…"       // feeds the existing POST /interactions/{id}/override loop
         }
  409 →  { code: "brain_not_ready", reason: "no_skills" }
```

`require_brain_access` already branches on credential kind (see
`app/shared/middleware/authorize.py:78`), so **one endpoint serves both** the
browser dashboard (JWT) and agents (API-key) — no separate route needed. Responses
serialize camelCase via `CamelModel`; the request rejects unknown keys via
`CamelRequestModel`.

### Service flow (transaction discipline)

1. **Gate** (Phase 0) — disabled/not-ready → `BrainNotReadyError`.
2. **Cache** — `cache.get_cached_search(ws, question)`; on hit, log interaction +
   return (mirrors `SkillsService.query`). Cache **only grounded answers**.
3. **Embed** the question — `embedder.embed_text` — **outside any txn**.
4. **txn A** (`run_in_tenant`, restricted `brain_app` pool): `similar_skills(top-K)`
   → for hits ≥ `0.70`, fetch full bodies (`SkillsRepository.get`, published-only)
   **+ the provenance dossier** (`BrainRepository.provenance`, see below). Commit.
   Now holding plain dicts, connection released.
5. **Synthesize** — `synthesizer.answer(question, retrieved_skills)` — **outside any
   txn** (Claude call). Returns `{answer, citations, confidence, grounded}`,
   `trust="skill"`.
6. **txn B**: `insert_interaction(...)` → `interaction_id`; **persist the turn** —
   `brain_messages` (user + assistant rows, with `confidence`/`sources`), creating a
   `brain_conversations` row if `conversationId` was absent; commit. Cache grounded
   answers (`set_cached_search`, 5-min TTL, already invalidated on publish).
7. Return the §7 payload (with `trust`, `conversationId`, `messageId`).

**Below-`0.70` behavior.** A miss returns an honest "I don't have a reviewed skill
for that yet" (`trust="none"`, `sources: []`) — never a synthesized guess. (Phase 3
widens retrieval to include each skill's **evidence** chunks, so lineage questions
resolve *within* a matched skill's graph rather than as a separate ungoverned tier.)
For agents via MCP, a miss still escalates to `run_query_extraction` as today.

### Synthesizer (the new "G")

- Reuse **`sonnet_json`** (`app/pipeline/llm/clients.py`) — `(system, user) →
  (dict, StageUsage)`, temp 0.0, retry + one reprompt. Perfect for a JSON answer
  envelope. Model `claude-sonnet-5` (`settings.anthropic_model`).
- **Grounding contract** (anti-hallucination): system prompt instructs the model to
  answer **only** from the supplied skill bodies, cite the skill(s) it used by id,
  and return `grounded: false` (→ we downgrade to `no_match`-style) if the context
  doesn't actually answer the question. Skills are passed as structured context
  (name, trigger, base_logic, exceptions, source_authority, confidence, version).
- **Confidence (0–100)** = blend of retrieval similarity (top hit) and the model's
  `grounded` self-check; never exceeds the retrieval similarity. Keeps the number
  honest rather than a model vanity score.
- **Citations** map each used skill → `{ provider, location, skillId }`. Provider
  comes from `skills.source_providers`; the granular location (channel/doc) and the
  human author come from the **provenance dossier** below, not from `source_authority`
  (which is an authority *tier* `high|medium|low`, not a location).

### Provenance & governance queries (who approved / what sources / who originated)

**Not every question is a semantic-similarity problem.** "What are the exceptions"
is answered by the retrieved skill text; **"who approved it," "what sources did it
come from," "who set it in motion," "when did it change"** are **structured audit
lookups** — the answer was never in the embedded `trigger + base_logic`, so vector
search can't surface it, and a synthesizer given only the skill text will **fabricate
a name/date**. For a governance answer that is a compliance liability, not a glitch.

So brain-query is **hybrid**: semantic search *finds* the skill; a metadata **join
keyed by `skill_id`** *explains* its governance. After retrieval (still in txn A),
fetch a **skill dossier** and (1) return it as structured fields and (2) pass it to
the synthesizer as **authoritative facts it may quote but must never infer**.

| Question | Ground-truth source (RLS-scoped, keyed by `skill_id`) |
|---|---|
| Exceptions | `skills.exceptions_block` (already retrieved) |
| What data sources | `skills.source_providers` (providers) + `skills.source_ids` → `source_events` → `source_connections`/`source_channels` (the `#channel` / doc name); per-exception `source_url` |
| Who **approved** | `reviews.resolved_by` where `skill_id=… AND verdict='approve'` (+ `resolved_at`); corroborated by `audit_log` (`action='skill.published'`, immutable) |
| Who **set it in motion** / originator | `reviews.evidence_author` or author in `source_events.payload`; **creator** = `skill_versions` (`change_type='create'`).`changed_by`; **last editor** = `skills.changed_by` |
| When it changed / history | `skill_versions` (`version`, `change_type`, `changed_by`, `created_at`) — the Phase-2 corpus |

New `BrainRepository.provenance(session, skill_id)` gathers this in one bundle of
parameter-bound, RLS-scoped queries (user ids resolved to display names). Enriched
response:

```jsonc
{ "answer": "…", "confidence": 96, "matchType": "semantic", "interactionId": "…",
  "sources":  [ { "provider": "slack", "location": "#cs-escalations", "skillId": "…" } ],
  "skillIds": ["…"],
  "provenance": {                          // structured — the FE renders, doesn't guess
    "approvedBy":  { "name": "Alice N.", "at": "2026-07-12T…" },
    "originatedBy":{ "name": "Jane D.", "via": "slack", "location": "#cs-escalations" },
    "createdBy":   { "name": "system", "changeType": "sweep_sourced" },
    "lastEditedBy":{ "name": "Bob R.", "at": "2026-07-20T…" }
  } }
```

**Honesty caveat — verify population, don't just trust columns exist:** the
*columns* are all present, but a couple depend on the write path actually setting
them. Before promising these in the API, confirm the approve path
(`app/modules/reviews/service.py`) sets `reviews.resolved_by`/`resolved_at` and
writes the `audit_log` row, and that extraction records the evidence author. Any
field not reliably populated is returned `null` (and the synthesizer says "not
recorded") — **never** back-filled by the LLM.

**Author-identity quality varies by provider (a real, scoped gap).** The originator
chain is fully wired end-to-end: `skill → source_event/source_ids → evidence_quote
+ evidence_author` (pipeline `skill_writer.py:118` writes both; the Slack **permalink
+ channel** are reconstructable from the stored `source_events.payload`). But the
*name* quality differs:

| Provider | `evidence_author` today | Human-readable? |
|---|---|---|
| GitHub | comment `user.login` (`expanders/github.py:40`) | ✅ e.g. `janedoe` |
| Google Drive | comment `author.displayName` (`expanders/google_drive.py:30`) | ✅ e.g. `Jane Doe` |
| Gmail | `From` sender (`gmail.py:273` actor email/name) | ✅ email/name |
| Jira | `_person(creator/reporter)` → name (`jira.py:463`) | ✅ display name |
| **Slack** | raw **user id** (`slack.py:503` `name:""`; `expanders/slack.py:46` `msg.get("user")`) | ❌ e.g. `U07A3B12` |
| **Zendesk** | numeric **`author_id`** (`expanders/zendesk.py:32`; `zendesk.py:297` `requester_id`, `name:""`) | ❌ e.g. `380288` |
| **Notion** | **no author fed to extraction** — expander emits page body only (`expanders/notion.py`); normalizer keeps `last_edited_by` **id**, `name:""` (`notion.py:239`) | ❌ usually **none** (page, not a person) |

So the four already-readable providers (GitHub, Drive, Gmail, Jira) answer "whose
comment/message" with a real name today. **Slack** and **Zendesk** return a raw
**id** (the quote + a link + the channel/ticket are solid — just the identity is an
id). **Notion** returns **no author** at all: a policy *page* isn't authored like a
chat message, so you get the page + link, not a person. **Fixes (connector-side,
small, independent of brain-chat):** Slack → resolve id via `users.info` (`users:read`
already granted, `slack.py:68`); Zendesk → resolve `author_id` via the Users API;
Notion → resolve `created_by`/`last_edited_by` id via the Users API and feed it to
the extractor. Tracked as **Open decision F**.

### Tests (Phase 1)

Service tests with **mocked repos/embedder/synthesizer/cache** (pattern:
`app/tests/skills/test_skills.py`): semantic-match synthesis, `no_match` (no
hallucinated answer), cache hit/miss, gate-disabled short-circuit, **provenance
dossier assembled correctly and a governance question with an unpopulated field
returns `null` (never a fabricated name)**. Router tests
with `get_auth_context` overridden + limiter disabled. **Mandatory** per `CLAUDE.md`:
cross-tenant negative test (workspace B can't read A's skills) and auth-failure
tests (missing/invalid key → 401, missing scope/role → 403, disabled → 409).

---

## Phase 2 — Versioned ingestion ("previous + new")

Bring **historical skill versions** into the chat corpus so the brain can answer
"what did our refund policy *used* to be / when did it change." Today
`skill_versions` has **no embedding**, so history is invisible to retrieval.

### Recommended store: a dedicated `brain_chunks` table

```sql
brain_chunks (                          -- unified chat index; shared by Phase 2 + Phase 3
  id            uuid pk,
  workspace_id  uuid not null,          -- RLS
  kind          text not null,          -- 'skill_version' (P2) | 'evidence' (P3) — both governed, skill-linked
  skill_id      uuid not null,          -- every chunk belongs to a skill
  version       text,                   -- skill_version kind
  is_current    boolean,                -- skill_version kind: true=live, false=superseded
  source_ref    jsonb,                  -- evidence kind: {provider, sourceItemId, url, label, author}
  chunk_index   int  not null,          -- long docs/skills → multiple chunks (recall + citation granularity)
  content       text not null,
  embedding     vector(1536),
  created_at    timestamptz default now()
)
-- HNSW cosine index on embedding; RLS SELECT policy (workspace_id = current_org_id()), system-writes.
-- All kinds are governed + skill-linked. Retrieval unions kind IN ('skill_version','evidence');
-- the answer's `trust` facet = 'skill' (the rule) vs 'evidence' (a cited source in its lineage).
```

**Why a dedicated table over adding `skill_versions.embedding`:**
- Cleanly separates the **chat corpus** from the operational `skills` table (no
  behavior change to the pipeline / delivery surface).
- Enables **chunking** long skills → better recall and precise chunk-level
  citations.
- Holds current + historical uniformly with an `is_current` flag.
- One clean **ingestion target** to build and gate on.

(Cheaper alternative if chunking isn't wanted for v1: add `embedding vector(1536)`
+ HNSW to `skill_versions` and `UNION` it into retrieval. Less work, no chunking,
no separate sync path. Called out as **Open decision B**.)

### Retrieval scope — current is authoritative

Default `brain/query` retrieves over `is_current = true` (answers reflect **today's**
policy). Historical chunks are retrieved **only** when the question is explicitly
temporal, and are always labeled *superseded* so the synthesizer never states an old
rule as current. This prevents stale versions from polluting live answers — the
whole point of a human-reviewed brain. (**Open decision A** — do versions *drive*
answers or only provide provenance/"what changed".)

### Ingestion job (the "ingest at runtime" backfill)

- New ARQ job `brain_index_backfill(workspace_id)` (`app/jobs/`): for every
  published skill and every `skill_versions` row lacking a chunk, chunk → embed
  (batched, **outside txn**) → upsert `brain_chunks`. **Idempotent** (skip
  already-chunked (skill_id, version)); safe to re-run and to run at boot.
- **Keep-fresh hook:** the same publish/approve path that already calls
  `cache.invalidate_skills(ws)` also enqueues incremental `brain_chunks` upserts for
  the new current version + demotes the prior version to `is_current = false`. No
  separate re-index cron needed.
- **Readiness tightens:** a workspace is ready when backfill has completed (track a
  per-workspace `brain_index_status` / `chunk_count`), not merely "≥1 embedding".

### Tests (Phase 2)

Backfill idempotency; current-vs-historical retrieval labeling; publish demotes
prior version + indexes new; cross-tenant isolation on `brain_chunks`; readiness
flips only after backfill completes.

---

## Phase 3 — Skill Evidence Graph (capture everything that fed a skill)

Make the whole lineage of every skill durable, resolved, and retrievable — the
source message/document, its author, the trigger, the exceptions, the reviewer — so
the brain answers **any** question about a skill *and how it came to be*, all governed
and skill-linked. Bounded by skills, not by ingested volume.

### What a skill's evidence graph contains

| Evidence node | Captured from | Answers |
|---|---|---|
| **Source content** (the deciding message / doc excerpt, ideally the fuller context, not one 500-char quote) | expander output at extraction (today discarded past 500 chars) | "what exactly was said" |
| **Author** (resolved to a name) | `evidence_author` + decision-F id→name | "whose message said we need this" |
| **Document / message** (provider, channel/page, url, item id) | `source_events` / `source_ids` | "what doc it came from" (+ deep-link) |
| **Trigger** | `skills.trigger` | "when does this apply" |
| **Exceptions** | `skills.exceptions_block` | "what are the carve-outs" |
| **Reviewer** (+ when) | `reviews.resolved_by` / `audit_log` | "who approved it" |
| **Version history** | `skill_versions` (Phase 2) | "when/how it changed" |

### Write-time capture (the real work)

Today evidence is partial (a 500-char quote), unresolved (raw author ids), and
scattered across audit rows. Phase 3 makes it complete at the moment a skill is
written/approved:

- **Persist the evidence, don't discard it.** At extraction, the expander already
  fetched the fuller source context (`orchestrator._expand`); capture the relevant
  span(s) into the evidence store instead of keeping only `evidence_quote[:500]`.
- **Resolve the people** (decision F): Slack/Zendesk/Notion author ids → names, so
  "who said it" is a name, not `U07A3B12`.
- **Index it:** upsert into the unified `brain_chunks` as `kind='evidence'`,
  `source_ref = {provider, sourceItemId, url, label, author}`, `skill_id`/`version`
  set, embedded — so evidence is retrievable *as part of the skill's knowledge*.
- **Backfill** existing skills' evidence via the `brain_builds` job (`queued→running→
  completed`, `progress`/`counts`) — the "ingest at runtime, show progress, enable
  chat when done" flow. Idempotent per `(skill_id, sourceItemId, chunk_index)`.

### Retrieval — governed, two facets

`BrainService.query` retrieves over skills **and** their evidence in one governed
index (`brain_chunks` `kind IN ('skill_version','evidence')` + live `skills`). The
answer's `trust` reflects the *facet* it used: `skill` (the reviewed rule) or
`evidence` (a cited source within a reviewed skill's lineage — still governed, but
flagged as "from the original source material"). No skill/evidence match → honest
`trust="none"`. Everything retrievable is skill-linked; there is no ungoverned tier.

### Boundaries (much smaller surface than a corpus)

Because we store only what fed a skill, the privacy/cost surface is inherently the
governed, relevant subset — no "every message is queryable." Still: evidence chunks
inherit the skill's RLS scope; deleting a skill cascade-deletes its evidence chunks;
author name resolution respects each connector's granted scopes.

### Tests (Phase 3)

Evidence captured at skill-write (author resolved, linked, embedded); a lineage
question ("who said we need this / who approved it / what doc") answers from evidence
with correct citations; `trust` facet is `skill` vs `evidence` correctly; backfill
idempotency + `brain_builds` lifecycle; cross-tenant isolation on `kind='evidence'`
chunks; skill-delete cascades evidence deletion.

---

## Cross-cutting compliance (every phase)

- **Identity:** `get_auth_context`; **authorize** with `require_brain_access("brain:query")`.
- **RLS:** every DB access inside `run_in_tenant` on the restricted `brain_app`
  pool (`get_tenant_session`). Never the privileged `get_session` pool.
- **Network I/O outside transactions:** embedding **and** synthesis calls happen
  between txns, never inside one (`CLAUDE.md` #7).
- **SQL:** parameter-bound only; lives in a repository; reuse `similar_skills`.
- **Envelope:** `ok()`; raise typed `AppError` (`BrainNotReadyError`), never bare
  `HTTPException`.
- **Rate-limit:** `@limiter.limit(BRAIN_LIMIT, key_func=workspace_key)` (120/min).
- **Cache:** reuse `app/pipeline/cache.py` (5-min TTL, invalidated on publish);
  cache grounded answers only — a cached `no_match`/`not_ready` would hide a skill
  added within the TTL.
- **Interactions + override loop:** every query logs `agent_interactions` and
  returns `interactionId`, so the existing `POST /interactions/{id}/override`
  feedback loop keeps working from the dashboard too.
- **Docs:** after routes/schemas land, run `make docs` and commit the regenerated
  `docs/openapi.json` + `docs/api.html`. Add tests in the same PR.

---

## Open decisions (my recommendation in **bold**)

- **A. Do historical versions drive answers, or only provenance?**
  **Recommendation: provenance-first** — current versions answer; historical chunks
  surface only for explicitly temporal questions, always labeled superseded. Avoids
  stale rules leaking into live answers.
- **B. Version store: dedicated `brain_chunks` (chunked) vs. `skill_versions.embedding` (simple union)?**
  **Recommendation: `brain_chunks`** for chunking + citation granularity + a clean
  ingestion/gate target. Fall back to the simple union only if we want the smallest
  possible Phase 2.
- **C. Synthesis model — `sonnet_json` (claude-sonnet-5) vs. `groq_json` (llama-3.3-70b)?**
  **Recommendation: `sonnet_json`** for answer quality/grounding; revisit Groq if
  p95 latency/cost demands it. Both are already wired.
- **D. Streaming?** §7 says a single JSON response is enough for v1.
  **Recommendation: JSON now**, add SSE later if the chat UX needs token streaming.
- **E. Readiness granularity — global kill-switch + per-workspace computed?**
  **Recommendation: both** (env kill-switch AND per-workspace readiness).
- **F. Resolve author ids → names for Slack / Zendesk / Notion?** GitHub, Drive,
  Gmail, Jira already give readable names; Slack + Zendesk give raw ids
  (`U07A3B12` / `380288`); Notion gives no author (page body only).
  **Recommendation: yes** — Slack via `users.info` (scope already granted), Zendesk
  via the Users API, Notion via `created_by`/`last_edited_by` + Users API. Small
  connector-side fixes, independent of the brain-chat build; ship brain-chat first
  (it shows id + link + quote, already useful) and land name resolution separately.
- **G. Scope — ✅ DECIDED: provenance-complete Skills-RAG (not a general corpus).**
  The brain answers from skills **plus each skill's evidence graph** (source message,
  author, document, reviewer, trigger, exceptions) — all governed, skill-linked,
  bounded by skill count. Phase 3 = the **Skill Evidence Graph** (capture + resolve +
  index evidence at skill-write). General Corpus-RAG over all ingested content is
  **out of scope** — which also removes the earlier retention/PII concern (we retain
  only what fed a reviewed skill).

---

## Sequenced task list

**Phase 0 — gate**
1. `settings.brain_chat_enabled`; `BrainNotReadyError` (409, typed).
2. `app/modules/brain/` skeleton; readiness helper (COUNT embedded published skills;
   later reads `brain_builds`).
3. `GET /brain/status` (returns `progress`/`current_step` from `brain_builds`) +
   router wiring in `app/main.py`; tests.

**Phase 1 — synthesis + endpoint + chat persistence (unblocks FE §7)**
4. `synthesizer.py` — grounded JSON prompt over `sonnet_json`; confidence blend.
5. `BrainService.query` — gate → cache → embed → `similar_skills` →
   `BrainRepository.provenance` (approver/sources/originator dossier) → synthesize
   (`trust="skill"`) → persist + log; two-phase txn discipline. Verify the approve
   path populates `reviews.resolved_by`/`audit_log`; unpopulated dossier fields → `null`.
6. `POST /brain/query` + schemas; rate-limit; persist turns to
   `brain_conversations`/`brain_messages`; interaction logging → `interactionId`.
7. Tests: semantic, no_match (no hallucination), cache, disabled, persistence,
   **cross-tenant**, **auth-failure**. `make docs`.

**Phase 2 — versioned skill ingestion**
8. Migration: unified `brain_chunks` (`kind`, + HNSW + RLS) — alembic, keeping
   `config.py`/`models/orm`.
9. `brain_index_backfill` ARQ job (idempotent; current + historical, `kind='skill_version'`).
10. Publish/approve hook: incremental upsert + demote prior version.
11. Add version-history retrieval; current-authoritative scoping; tighten readiness
    to "backfill complete".
12. Tests: idempotency, current-vs-historical, publish-demotes, cross-tenant. `make docs`.

**Phase 3 — Skill Evidence Graph (capture everything that fed a skill)**
13. Write-time evidence capture: at skill write/approve, persist the source span(s)
    (not just `evidence_quote[:500]`) + reviewer + document ref into
    `brain_chunks(kind='evidence')`, embedded; author id→name resolution (decision F).
14. `brain_build` ARQ job: backfill evidence for existing skills; progress/counts;
    idempotent per `(skill_id, sourceItemId, chunk_index)`.
15. Widen retrieval to union `kind IN ('skill_version','evidence')`; set the `trust`
    facet (`skill` vs `evidence`); citations from `source_ref`.
16. Skill-delete cascades evidence-chunk deletion; keep-fresh on new version/approve.
17. Tests: evidence captured+resolved+linked; lineage questions answer from evidence
    with citations; `trust` facet; backfill idempotency; cross-tenant. `make docs`.

---

## What this is **not**

- Not a new vector database — retrieval stays in Postgres/pgvector (RLS + freshness
  + cache are the reason).
- Not a change to the MCP `query_brain` contract — agents keep their existing
  tool; the dashboard gets a JWT REST sibling that reuses the same core.
- Not query-driven live extraction (Feature 16) — that remains its own remaining
  item; `no_match` still escalates to it on the agent path unchanged.
