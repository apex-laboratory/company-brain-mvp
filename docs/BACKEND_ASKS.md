# Backend asks — Frontend integration gaps

What the frontend needs from `brain-api` to finish wiring the dashboard. Surfaced
while integrating `features/` against the real API (branch `be/integrations`).

Two kinds of gap:

- **Part A — Skills registry** (§1–6): the Skills feature is wired, but parts are
  search-only / metric-less / simulated because endpoints are missing.
- **Part B — Entirely missing surfaces** (§7–8): dashboard features with **no
  backend module at all** — they run on mock data today and cannot be wired until
  the backend exists.

Everything below assumes the existing conventions: `/api/v1` prefix, camelCase
`{ data, meta }` success envelope, JWT (dashboard, role `viewer<editor<admin`) or
`X-API-Key` (agents) auth.

## Context: what's already wired

| Method | Path | FE usage |
|--------|------|----------|
| GET | `/skills/search` | Registry search (`useSkillsSearch`) |
| GET | `/skills/{id}` | Wired in API layer; no UI yet |
| GET | `/skills/{id}/versions` | Wired in API layer; no UI yet |
| GET | `/skills/export` | Export button (`useExportSkills`) |
| POST | `/interactions/{id}/override` | Wired in API layer; agent-facing, no UI |

The gaps below are the reason parts of the UI are search-only, missing metrics,
or simulated.

---

## 1. List / browse skills without a query — **high priority**

**Problem.** There is no list-all endpoint — only `/skills/search`, which
requires `q` (min length 1). So the registry page cannot show anything until the
user types. The default "browse the registry" view is impossible today.

**Ask.** A paginated list endpoint:

```
GET /api/v1/skills?status=&source=&limit=&cursor=
```

Response: an array of the same shape as a search hit (minus `similarity`), plus
pagination `meta`. Reuse `SkillSearchResult` fields where possible so the FE can
share one view model.

**FE impact.** `useSkillsSearch` would fall back to this list when the query is
empty, restoring a default registry view instead of the current "type to search"
empty state.

---

## 2. Usage metrics on skills — **medium priority**

**Problem.** The registry table has columns for **call count (30d)**, a **call
sparkline**, and **last-updated**, but neither `SkillSearchResult` nor `SkillOut`
carries usage data. The FE currently renders a placeholder / the `% match` score
instead. `SkillOut` has `updatedAt`, but search hits do not.

**Ask.** Either:

- add usage fields to `SkillSearchResult` / the list item, e.g.
  ```jsonc
  {
    "calls30d": 2100,          // int
    "callSeries": [120, 160, 180, 210, 240, 260, 290], // 7-point sparkline
    "updatedAt": "2026-07-21T10:00:00Z"
  }
  ```
- **or** expose a separate `GET /api/v1/skills/{id}/usage` if metrics are
  expensive to compute inline.

Formatting (`2.1k`, `2d ago`) stays on the FE — send raw numbers / ISO
timestamps.

---

## 3. Registry summary stats — **medium priority**

**Problem.** The page header used to show a stat strip (Total / Stable / In
review / Calls·30d). Those were hardcoded and have been removed, because there's
no endpoint behind them.

**Ask.** A small aggregate endpoint, mirroring `/reviews/stats`:

```
GET /api/v1/skills/stats
→ { "total": 37, "stable": 31, "inReview": 4, "draft": 2, "calls30d": 11600 }
```

**FE impact.** Restores an honest stat strip on the Skills page.

---

## 4. `status` on search results — **low priority**

**Problem.** `SkillSearchResult` has no `status` field (only `SkillOut` does).
The registry table shows a status badge, so the FE currently **defaults every
search hit to `stable`** on the assumption that search only returns published
skills. If that assumption is wrong, the badge is misleading.

**Ask.** Add `status: string` to `SkillSearchResult` (and to the list item from
ask #1), or confirm in writing that search only ever returns `stable`/published
skills so we can keep the default.

---

## 5. Create a skill — **low priority (product decision)**

**Problem.** The "New skill" dialog (`NewSkillDialog`) is **simulated** — it only
fires a toast. There is no `POST /skills`. Skills appear to be created by the
extraction pipeline + review flow, not by hand.

**Ask.** Decide the intended behavior:

- **(a)** If manual creation isn't a product goal, we'll remove the "New skill"
  button (or repoint it at "connect a source" / the review queue).
- **(b)** If it is, we need:
  ```
  POST /api/v1/skills   { name, trigger?, baseLogic } → SkillOut (status "draft")
  ```
  (admin-only), landing the skill in the review queue.

Flag which one; today the button promises something the backend can't do.

---

## 6. Override from the dashboard — **low priority (clarification)**

`POST /interactions/{id}/override` is gated on the `skills:invoke` scope — i.e.
the **agent** surface. It's wired in the FE API layer but has no dashboard UI,
because a human reviewer has no natural place to "report an override."

**Ask.** Confirm whether a dashboard admin (JWT) should ever call this. If yes,
confirm `require_brain_access("skills:invoke")` accepts an admin JWT (role gate),
and point us at the intended UI entry point. If no, we'll leave it agent-only.

---

# Part B — Entirely missing surfaces

These dashboard features have **no backend module at all**. They ship today on
static mock data and cannot be wired until the backend exists. Flagged at the
start of the integration pass.

## 7. Brain query (the "Ask the brain" chat) — **high priority**

**Problem.** `features/brain-chat/` is a full chat UI (`BrainChatPage`,
`useBrainChat`) that answers operational questions with a cited, confidence-scored
answer. It runs entirely on local regex matching over a static answer list
(`data/brain-answers.ts`) — there is **no dashboard endpoint** behind it.

The capability exists on the backend but not where the dashboard can reach it:
`SkillsService.query(auth, situation)` and the `query_brain` **MCP tool** (its own
process on port 8001, authenticated by `X-API-Key` + `brain:query`). A browser
dashboard user (JWT) has nothing to call.

**Ask.** A REST wrapper over the existing query logic, JWT-authenticated:

```
POST /api/v1/brain/query   { "question": "..." }
→ {
    "answer": "Premium customers have a 45-day refund window…",
    "sources": [ { "provider": "notion", "location": "Policy Library" }, … ],
    "confidence": 96,          // 0–100
    "skillIds": ["…"]          // optional: skills the answer drew on
  }
```

Should reuse `SkillsService.query` so the MCP tool and the dashboard stay in sync.
Streaming is optional — a single JSON response is enough for v1.

**FE impact.** Replaces the mock `answerFor()` with a real call; the chat becomes
the actual brain query surface instead of a canned demo.

## 8. Decisions registry — **medium priority (needs product decision first)**

**Problem.** `features/decisions/` (`DecisionsPage`, `DecisionRow`,
`DecisionDetail`) lists operational **decisions** — title, source + location,
status, confidence, category, owner, usage count, last-updated, body, and an
executable **rule** (pseudocode). It's 100% mock (`data/decisions.ts`); there is
no `decisions` module in `app/modules/`.

**Open question — is this a distinct concept or a view of skills?** A "decision"
overlaps heavily with a published **skill** (rule + source lineage + confidence +
status). Before building anything, confirm one of:

- **(a)** Decisions *are* skills presented differently → we point this page at the
  skills endpoints (needs §1 list-all + §2 metrics + an `owner`/`category`/`rule`
  field on the skill payload), and no new module is required.
- **(b)** Decisions are a separate domain object → we need a new module:
  ```
  GET  /api/v1/decisions?status=&category=&source=&limit=&cursor=
  GET  /api/v1/decisions/{id}
  ```
  returning `{ id, title, provider, location, status, confidence, category,
  owner, uses, updatedAt, body, rule }`.

**FE impact.** Until this is decided, the Decisions page stays on mock data. It's
the last fully-simulated screen in the dashboard.

---

## Priority summary

| # | Ask | Priority | Unblocks |
|---|-----|----------|----------|
| 7 | `POST /brain/query` (JWT wrapper over `query_brain`) | **High** | Brain-chat becomes real (whole feature) |
| 1 | `GET /skills` list-all | **High** | Default registry (browse without searching) |
| 8 | Decisions: reuse skills **or** new `decisions` module | Medium | Unblocks the Decisions page (whole feature) |
| 2 | Usage metrics (calls / sparkline / updatedAt) | Medium | Real table metrics |
| 3 | `GET /skills/stats` | Medium | Header stat strip |
| 4 | `status` on search results | Low | Accurate status badges |
| 5 | `POST /skills` **or** remove "New skill" | Low | Manual skill creation (or honest UI) |
| 6 | Override-from-dashboard clarification | Low | Decide if a UI is needed |

**Whole features blocked:** §7 (brain-chat) and §8 (decisions) are entire
dashboard screens with no backend — the highest-leverage asks. §1–6 are polish on
the already-wired Skills feature.
