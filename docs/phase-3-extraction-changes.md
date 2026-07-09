# Phase 3 — Extraction Pipeline: Change Log

Branch: `feature/phase-3-extraction` · Commits: `471ad5b` → HEAD · Tests: **483 unit + 2 E2E passing, ruff clean**

Phase 3 turns ingested `source_events` into published/reviewable **skills** via a
6-step LLM pipeline (PRD Features 8–10): relevance gate → context expansion →
decision identification → skill extraction → boundary classification →
routing/write, with contradiction detection, human review, cost tracking, and
dead-lettering throughout.

---

## M1 — Pipeline skeleton, LLM clients, authority (`471ad5b`)

New `app/pipeline/` package foundations:

| Component | What it does |
|---|---|
| `types.py` | Stage DTOs (`GateResult`, `IdentifiedDecision`, `SkillDraft`, `PipelineResult`, …) + `CostLedger` accumulating per-stage LLM spend |
| `llm/retry.py` | Transient-aware backoff (429/5xx/timeouts retry; 4xx fail fast) |
| `llm/clients.py` | JSON-mode Groq + Sonnet wrappers with **one reprompt** on malformed JSON; every call captures `$`/Mtok cost |
| `llm/pricing.py` | Per-model $/Mtok tables |
| `embedder.py` | `text-embedding-3-small`, **1536-dim pinned** (matches the pgvector column) |
| `authority.py` | `source_authority.yaml` port — authority annotation + routing/sweep config, **fail-soft** (missing/broken YAML degrades to defaults, never crashes the worker) |
| `confidence_scorer.py` | PRD Feature 9 multipliers, ported verbatim |
| `cache.py` | Best-effort `skills:{ws}:*` Redis invalidation |

- **Migration `0014` (was 0013, renumbered at the main merge)**: `source_events.attempts` +
  `pipeline_meta` (per-stage costs, stage reached, last error) + partial index on failed
  events; `reviews.payload` JSONB for structured contradiction cards. ORM mirrors updated.
- **Settings**: pipeline LLM keys/models (empty defaults; fail at first use, not at boot).
- 44 unit tests (retry matrix, pricing, JSON reprompt, authority fail-soft).

## M2 — Happy-path extraction + ARQ wiring (`e04c11d`)

- **Stages** (all mocked at the `llm.clients` boundary in tests):
  - `relevance_gate` — Groq binary "is this a decision-bearing event?"
  - `decision_identifier` — Groq Pass 1; non-threaded providers wrap content **without** an LLM call
  - `skill_extractor` — Sonnet Pass 2 → `SkillDraft`
  - `skill_writer` — routing table: `published→active` / `review→review + reviews row` / `draft`; confidence scaling; cache invalidation on publish
- **`PipelineRepository`** — load/finalize event, pgvector-ready skill insert, version +
  review writes, name dedupe, sweep counters. Prompts live as constants in `pipeline/prompts/`.
- **`orchestrator.run_pipeline`** — stage sequencing + `CostLedger`; connection discipline:
  *short read tx → connectionless LLM stages → single write tx* (never holds a DB
  connection across an LLM call).
- **`extract_event` ARQ task** — dead-letters failures (`outcome='failed'`, `attempts+1`,
  error meta) and never re-raises; registered in the worker.
- **Ingest integration**: `insert_event` now RETURNs the event id and takes `sweep_id`;
  `webhook_ingest` and non-sweep `source_sync` enqueue `extract_event` per inserted event;
  sweep syncs stamp `sweep_id` and defer to the batched `sweep_extract` (M3).

## M3 — Boundary classifier, contradiction detector, sweep_extract (`4519947`)

- **`boundary_classifier`** — pgvector top-3 cosine search (`similar_skills`, HNSW index):
  below the **0.82** threshold → `NEW` with **no LLM call**; above → Groq 4-way
  `UPDATE / EXCEPTION / DUPLICATE / NEW`. Sweep scope also matches `status='review'`
  so a sweep's own pending skills dedupe against each other (PRD sweep-scope rule).
- **`contradiction_detector`** — Sonnet compares proposed vs existing `base_logic`.
- **`skill_writer` routes**:
  - `write_duplicate` — append source id, stop
  - `write_update` — published: mutate + version + re-embed + cache invalidate; review-status: `policy_change` review row, **no mutation**
  - `write_exception` — append carve-out to `exceptions_block`, `base_logic` untouched
  - `write_contradiction` — two-source review card, no mutation
- **Orchestrator** — `boundary → _route` dispatch; `UPDATE` runs contradiction detection
  (true → contradiction review, false → update); `DUPLICATE` short-circuits. Dead-letter
  centralized in `run_event_safely` (shared by `extract_event` + `sweep_extract`).
  `PipelineResult` carries `cost_usd` for the sweep rollup.
- **`sweep_extract` job** — authority-ordered, semaphore-bounded, rate-paced batch
  extraction; `sweep_sourced=True`; progress + cost rolled into
  `sweeps.progress['extraction']`; **resumable** (re-selects queued events).
  `onboarding_sweep` enqueues it after ingestion.

## M4 — Context expanders, KAN-10–14 (`c3f6b13`)

- **`pipeline/expanders/`** — `ExpandRequest` bundle, `ContextExpander` protocol,
  provider registry, passthrough for gmail. Six expanders reuse each connector's HTTP idioms:
  | Provider | Enrichment |
  |---|---|
  | slack | `conversations.replies` — full thread + reactions/pins |
  | notion | recursive block children (bounded budget) → full page body |
  | github | issue/PR comments via the item's `comments_url` |
  | zendesk | ticket comments (subdomain from `payload._subdomain`) |
  | jira | issue comments via API gateway (cloudId = account id), ADF→text reused |
  | google_drive | file comments appended to the exported doc body |
- **`jobs/token_helper.py`** — `resolve_token` (moved out of `source_sync`; refresh + persist)
  and `token_for_provider` (connection lookup by workspace+provider) so expanders share
  the exact sync-path refresh semantics.
- **Orchestrator** — `_expand` runs post-gate/pre-identifier; **best-effort**: on failure
  falls back to raw content and records `pipeline_meta.expander_error` — never fatal.

## M5 — Review approve/reject API (`0ddc885`)

- **`app/modules/reviews/`** (router/service/repository/schemas, mounted at `/api/v1`, admin-only):
  - `GET /reviews?status=&kind=&limit=` — queue, contradictions first
  - `GET /reviews/stats` — approved/rejected/pending + rejection rate (PRD §15)
  - `POST /reviews/{id}/approve` · `/reject` (optional `{comment}`)
- **Approve semantics per kind** (reuses `PipelineRepository`, so an approval produces the
  same skill state as an auto-publish; confidence forced to **1.0**, human-confirmed):
  | Kind | Effect |
  |---|---|
  | `new_decision` | skill `review→active` + v1 version row |
  | `policy_change` | `after_text` becomes new `base_logic`, version bump, re-embed |
  | `exception` | carve-out appended to `exceptions_block`, version bump, logic kept |
  | `contradiction` | new source accepted (applied as an update) |
- **Reject** records the verdict; only a `new_decision`'s own review-status skill is demoted
  to `draft` (stops matching sweep-scope boundary search). Other kinds never mutated the
  live skill, so it stays intact. Non-pending → `409`; unknown → `404`.

## Merge of main / PR #9 review fixes (`487378d`, 2026-07-08)

Main (with the kan-2 connectors + all PR #9 review fixes) merged into Phase 3.
Conflict resolutions worth knowing:

- **`source_sync.py`** — kept Phase 3's `sweep_id` param, `token_helper.resolve_token`,
  and per-event `extract_event` enqueue; combined with main's rate-limit-aware 403
  classification (`_is_rate_limited`) and Gmail/Drive backfill chunk chaining.
  **`sweep_id` is threaded through chained backfill runs** so sweep-stamped events keep
  deferring to the batched `sweep_extract` instead of leaking per-event extractions.
- **`webhook_ingest.py`** — main's multi-workspace fan-out (`resolve_all_by_account`)
  combined with Phase 3's extraction: each workspace's inserted event enqueues its own
  `extract_event`.
- **Alembic** — Phase 3's `pipeline_deadletter` migration renumbered `0013 → 0014`
  (main's `0013` `NULLS NOT DISTINCT` migration is published). Chain verified linear,
  single head `0014`.

## M6 — Synthetic validation dataset + eval harness (2026-07-10)

The last unbuilt PRD deliverable: **extraction quality measured, not assumed**.

- **`brain-api/evals/`** — labeled synthetic datasets + a runner that executes the
  *real* stage functions (real prompts, real LLM calls) and scores them against the
  PRD gates:
  | Dataset | Items | Metric | PRD gate |
  |---|---|---|---|
  | `datasets/relevance.jsonl` | 24 (12 relevant / 12 chatter, 6 providers) | precision (+recall/accuracy) | **precision ≥ 70%** |
  | `datasets/boundary.jsonl` | 14 (all 4 labels incl. below-threshold no-LLM NEW cases) | accuracy + per-label recall | reported, not gated |
  | `datasets/contradiction.jsonl` | 10 (5 conflicts / 5 refinements) | recall (+precision) | **recall ≥ 80%** |
- **CLI**: `cd brain-api && python -m evals` (or `--suite relevance --json`); exits
  non-zero when a gate fails — this is the regression suite for every prompt change.
  Requires real `GROQ_API_KEY`/`ANTHROPIC_API_KEY` (it measures prompt quality; there
  is nothing meaningful to run against mocks).
- **Harness logic unit-tested** with fakes (`app/tests/evals/`): metric math, gate
  evaluation, error-counts-as-miss, the boundary no-LLM path, dataset validity.
- Rejection-rate instrumentation (PRD §15's third gate, ≤ 25%) already ships via
  `GET /reviews/stats` (M5).

## End-to-end acceptance suite (2026-07-10)

`app/tests/e2e/` — the full Phase 3 acceptance run against a **real
Postgres+pgvector** with all migrations applied. Everything is real (ingest via the
Slack connector, `sweep_extract`/`extract_event`, orchestrator, stages, retry/backoff,
JSON reprompt, pgvector HNSW boundary search, reviews HTTP API); only the outermost
LLM/embedding transports are scripted via a marker DSL (see `e2e/conftest.py`).

Covered PRD acceptance criteria, all passing:
- sweep produces review-queue skills; nothing auto-publishes during a sweep
- restated policy classified **DUPLICATE against a still-pending skill** (sweep scope)
- contradiction detected on the UPDATE route → review card with **both sources populated**
- every written skill has a non-null 1536-dim embedding
- forced transient LLM failure (429) retried by the real backoff and succeeds
- forced permanent failure dead-letters (`outcome='failed'`, attempts+1, error meta)
  and is visible in `sweeps.progress['extraction'].failed`
- per-event stage costs + per-sweep `cost_usd` rollup reported
- approve (new_decision → active + v1; contradiction → v2 re-embedded logic swap),
  reject (demotes review skill to draft), double-resolve 409, stats rejection rate
- non-sweep path: confidence ≥ 0.90 at ≥ medium authority auto-publishes with no review

Run it (see `e2e/conftest.py` header for the full recipe):

    docker run -d --name brain-e2e-pg -e POSTGRES_PASSWORD=e2e -p 55432:5432 pgvector/pgvector:pg16
    DATABASE_URL=postgresql+asyncpg://postgres:e2e@localhost:55432/postgres python -m alembic upgrade head
    E2E=1 DATABASE_URL=postgresql+asyncpg://postgres:e2e@localhost:55432/postgres python -m pytest app/tests/e2e/ -q

To run against the Supabase project instead (pre-launch, no live data): bring its
migrations to head the same way, then add `E2E_ALLOW_REMOTE=1` (the suite TRUNCATEs
tables, so this must never point at real data). As of 2026-07-10 the project
`zimxokenvfsovnmnyesf` is paused/unreachable — restore it from the dashboard first.

**Real bug found by the suite:** `GET /reviews` with no filters 500'd —
`ReviewsRepository.list` bound NULL `status`/`kind` params without casts, and asyncpg
can't infer NULL parameter types (`AmbiguousParameterError`). Unit tests stubbed the
repository, so only the E2E run caught it. Fixed with explicit `CAST(... AS text)` in
the NULL checks.

The fresh-database migration run (`alembic upgrade head`, 0001→0014) is itself part
of the validation: it required shimming Supabase's `auth.uid()` (created by the
platform, referenced by migration 0002) — documented in the E2E recipe.

## Architectural invariants (hold these in future work)

1. **No DB connection across an LLM call** — read tx, then LLM stages, then one write tx.
2. **Dead-letter, never crash** — pipeline failures mark the event failed with stage/error
   meta; ARQ jobs don't re-raise pipeline errors.
3. **Cheap-first LLM routing** — Groq for gate/identify/boundary; Sonnet only for
   extraction and contradiction; below-threshold boundary skips the LLM entirely.
4. **Review rows never mutate live skills** — only approve does, via the same repo paths
   as auto-publish.
5. **Sweep vs live extraction** — sweep events are batched/rate-paced via `sweep_extract`;
   webhook/poll events extract immediately per event.
6. **Expansion is best-effort** — an expander failure degrades to raw content, recorded in
   `pipeline_meta`, never fatal.
