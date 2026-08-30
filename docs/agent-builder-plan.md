# Agent Builder — FE + BE implementation plan

User-built agents that reach their own tools at runtime, grounded in the Brain,
with connector data never landing on Brainite infrastructure.

Spans both repos: `company-brain-mvp` (backend) and `brainites-fe` (frontend).
Runtime is **Anthropic Managed Agents**. Estimate: **~3 weeks with two engineers
in parallel**, ~5 solo.

---

## 1. Decisions locked

| Question | Decision | Consequence |
|---|---|---|
| What "custom connector" means | **Runtime, not ingestion.** A user's agent reaches their tools live during a run. | Nothing is pulled into the Brain. Schema stays generic enough that an ingestion flavor can reuse it later. |
| What v1 is | **A shippable surface.** Agent CRUD + versioning, builder UI, per-connector OAuth, vaulting, run history. | Not a spike. |
| How agents are invoked | **Chat, schedule, and run-now.** | Anthropic's scheduled deployments cover cron *and* manual triggers, so this costs far less than it looks. |
| Who can build and see them | **Personal, then published.** Any member builds; private by default; explicit publish-to-workspace. | Needs `owner_user_id` + `visibility` in the RLS policy. |
| Whether agent runs feed the Brain | **Yes, eventually — a stated v1 non-goal, not an open question.** Builder runs will produce Feature 29 envelopes from event-stream metadata. | No schema change and no v1 scope (§4.6). Phase 4's stream proxy gets written knowing a metadata tap is coming. |

Everything below follows from one boundary: **Brainite stores pointers to
agents, sessions, vaults and schedules — never the content that flows through
them.** Connector data goes MCP → Anthropic and back, and never transits our
process. That's what buys the liability split, and it's why the plan is smaller
than the original debate assumed.

---

## 2. How we build it

This ships into two codebases that already have strong conventions. The plan
adds no new architectural patterns; it follows the ones that are there.

**Match the existing shape, don't invent one.**

- Backend: `router` (paths + deps only) -> `service` (logic) -> `repository`
  (the only place SQL lives), per `BACKEND_BEST_PRACTICES.md`. Response schemas
  are `CamelModel`; request schemas are `CamelRequestModel` and reject unknown
  keys.
- Frontend: one feature slice under `src/features/agents/` with
  `api/ hooks/ components/ pages/`, exactly like `features/sources/`. Nothing
  agent-specific leaks into `src/lib` or `src/components/shared` until a second
  feature needs it.

**One seam per vendor.** Every Anthropic call goes through
`anthropic_client.py`; nothing else imports the SDK. That keeps the surface
mockable in tests, makes retry and rate-limit policy one file's problem, and
contains the Managed-Agents lock-in noted in §8 to a module rather than
smearing it across the service layer.

**Connectors are data, not code.** A new provider is a row in
`agent_connectors.yaml` plus an OAuth client registration — never a new Python
class. This is the deliberate difference from the ingestion connectors
(compile-time `SourceIntegration` implementations); don't copy that pattern
here.

**No abstraction before the second use.** The four OAuth flows will look
similar and won't be identical. Write them out, then factor what actually
repeats. A premature `BaseOAuthProvider` costs more than the duplication it
removes.

**Boundaries enforced by tests, not comments.** The data-liability wall (§5.5)
is an import-guard test and a schema test. A rule that exists only in prose is
a rule that gets broken under deadline.

**Every route ships with its tests in the same PR** — unit tests against mocked
repositories, plus a cross-tenant negative and an auth-failure case. That is the
existing convention and the reason RLS regressions haven't shipped.

**Migrations are additive.** Six new tables, no changes to existing ones. The
`agent_*` names are scoped so nothing collides with `agent_interactions` (§4.5).

---

## 3. What we don't have to build

Four things we'd otherwise write ourselves already exist as Anthropic primitives.

| We'd have built | Instead we get | What's left for us |
|---|---|---|
| A credential store with token refresh | **Vaults** — credentials keyed by MCP server URL, auto-refreshed via the OAuth `refresh_token` grant, injected at egress. They never enter the sandbox. | Run the OAuth dance once, POST the tokens to a vault, keep the `vlt_` id. |
| A cron scheduler with run records | **Scheduled deployments** — cron + IANA timezone, one session per firing, a `drun_` record per attempt *including failures*, pause/unpause, manual run endpoint. | Store the `depl_` id, build the cron picker. |
| Agent config versioning and rollback | **Agent objects** — every update mints an immutable version; sessions pin to one. Optimistic concurrency via `version`, 409 on conflict. | Mirror `agent_id` + `version`; surface a version list. |
| A connector integration per provider | **Hosted MCP servers** — declare `{type, name, url}` on the agent. No SDK, no polling, no schema mapping. | Curate a catalog, allow a pasted URL. |

What's genuinely ours: the **per-connector OAuth connect flow** (still the hidden
cost — four providers, four consent screens, four token endpoints), the
**builder UI**, **ownership + visibility under RLS**, and **six pointer tables**.
Everything Claude-shaped is configuration.

---

## 4. Blockers in the current code

Found by reading the repo. Two are one-line fixes; the rest are decisions, and
all of them are now settled — §4.1, §4.4, §4.5 and §4.6 record the call, not the
debate.

### 4.1 The MCP server speaks the wrong transport

`app/mcp/server.py:122` runs `transport="sse"`. Managed Agents connects to MCP
servers over **Streamable HTTP**.

**Decided: switch to `transport="http"`. Do not dual-mount.** The compatibility
argument for keeping SSE is hypothetical — the plugin is unpublished, so there is
no installed base to protect. `plugin/.mcp.json` did not exist when this was
settled (`plugin.json` pointed at a missing file), so nothing declares SSE today
and the switch costs one line in a file we are writing anyway. Against that:
this MCP server is about to become the only internet-facing service carrying an
API key, and two transports means two auth paths and two rate-limit surfaces to
get wrong on day one. SSE is also the deprecated MCP transport, so carrying it
forward means migrating twice.

Sequencing consequence: **do not publish the plugin to a marketplace before
phase 0.** Publishing against `/sse` creates exactly the installed base whose
absence makes this cheap. Marketplace submission is gated on the same HTTPS
ingress phase 0 delivers (§4.3) — one ingress unblocks both efforts.

Still open: confirm which transport the OpenAI plugin directory accepts before
freezing the submitted URL, rather than inheriting it from `PLUGIN_BUILD.md` —
whose record on unverified platform details is seven errors deep.

### 4.2 The MCP server only accepts `X-API-Key`

`_authenticate()` at `app/mcp/server.py:62` reads exactly one header. A vault
`static_bearer` credential arrives as `Authorization: Bearer <token>`. Accept
both, resolving through the same `authenticate_api_key` path so the scope check
is unchanged.

### 4.3 It isn't publicly reachable

Port 8001 is a compose-internal service. Anthropic's MCP proxy has to reach it
over HTTPS, so it needs its own ingress (`mcp.brainite.…`), with the existing
per-IP throttle doing the work it already does.

**This ingress is also the blocker on plugin distribution.** `PLUGIN_BUILD.md`
originally claimed the endpoint was already public; it is not, and both the Claude
marketplace and the OpenAI directory require a public HTTPS MCP host. One ingress
closes two efforts — worth knowing when phase 0 gets squeezed.

### 4.4 ⚠️ The real hole in the wall

`query_brain` escalates a miss into `run_query_extraction`, which writes skills
and logs the query into `agent_interactions`. Attach it to a user's agent — which
is the whole moat play — and the agent's *query text* starts landing in the
Brain. An agent that has just read a Slack DM will phrase its query using that
content. **That is connector data crossing the wall by the back door.**

Fix: an `agent_origin` flag that skips `run_query_extraction` and nulls the
`query` column while still recording the match.

**Decided: scope it wider than the agent builder.** The flag is per-credential
and explicit, defaulting **on** for anything that is not a dashboard JWT —
plugin keys, agent-session keys, CI harnesses alike. Scoping it to agent
sessions only would leave the same door open on every other agent credential.

The plugin case looks weaker at first and is not. Its user explicitly ran
`brain enable` in that repo and already ships their prompt as the `task` field
of every run, so nulling a query column while sending the same text elsewhere
would look like theatre. But it is not the same text: the plugin's redaction
contract is **digests, never contents** — file paths, never file bodies; a hash
of a shell result, never the result. A model-issued `query_brain` call is the
one channel that can carry file contents into `agent_interactions` verbatim.
Closing it is what makes the promise true rather than approximately true.

What settles it is that the cost today is zero. `search_sources()` returns `[]`
(`app/pipeline/query_extraction.py:48`) — no provider implements search yet — so
`run_query_extraction` cannot currently mint a skill from anything. We give up a
feature that does not function. "Cheap now, expensive to retrofit" applies with
more force here than in the case it was written for.

What it closes is one write site: `app/modules/skills/repository.py:495` puts a
raw `query` column on every match, and that is the only place plugin- or
agent-captured file contents could land unredacted.

Not affected: reinforcement (F34) reads the `mcp__brainite__query_brain` *step*
in the trajectory, which the plugin records separately; usage counters and the
override flow read the match, not the query text. The read-side cache is keyed
on a sha256 digest of the query (`app/pipeline/cache.py:36`), so it never stores
query text either.

### 4.5 Naming collision

`models/orm/agent.py` already holds `AgentInteraction` (table
`agent_interactions`) — the extraction pipeline's episodic log, unrelated to this
feature. Put the new ORM models in **`models/orm/agent_builder.py`**.

Three `agent_*` concepts will coexist once this lands:

| Table | Is | Written by |
|-------|-----|-----------|
| `agent_interactions` | the episodic query log | extraction pipeline / `query_brain` |
| `agent_runs` | ingested coding-agent traces (Phase 7) | `POST /runs`, the plugin |
| `agent_sessions` | agent-builder run pointers | this feature |

The last two are the pair that will get confused. **Say so in the migration's
comment**, not only by putting the ORM in a separate file — the next engineer
reads the migration, not the filename.

### 4.6 Agent-builder runs must feed the corpus

**Decided now, built after v1.** No schema change and no v1 scope: the Phase 7
`harness` literal already accepts `"custom"` (`app/modules/runs/schemas.py:81`),
so there is no migration and no new endpoint. What the contract freeze needs is
the stated intent, so phase 4's stream proxy is written knowing a metadata tap
is coming.

Grounding (`query_brain` on by default, §10) is **read-only**. It makes agents
better today using knowledge that came from somewhere else, and it is precisely
the feature Anthropic can ship themselves. A loop that compounds is not.

The discard also falls on the best data. Engineers use the plugin; ops and
support people use the agent builder. The operational knowledge this product
exists to extract — refund handling, escalation policy — comes from the ops
side. If agent-builder runs never feed the corpus, the highest-value runs are
the ones thrown away and the loop is fed only by Claude Code users.

**The wall survives this.** The Feature 29 envelope was designed for exactly
this constraint: results are a 16-character hash plus a byte count, arguments go
through the redactor, and `task` is a prompt the user typed into our own UI. A
step reading `mcp__slack__read_message` with redacted args and a result digest
carries no Slack content.

Build it as a derivation from **event-stream metadata** — event types, tool
names, statuses, result sizes — never from transcripts. The CI guard in §5.5
then gets sharper, not weaker: *the envelope builder may read event metadata,
never event content.* `agent_sessions` still stores nothing.

Deferring this silently is the risk. Discovering at scale that the platform's
own agents never taught the Brain anything is a strategy problem, not a backlog
item.

---

## 5. Backend

One module in the existing shape: `router` → `service` → `repository`, RLS on
every read, camelCase out. See `BACKEND_BEST_PRACTICES.md`.

### 5.1 Data model — six tables, no content columns

| Table | Holds | Deliberately absent |
|---|---|---|
| `agent_definitions` | `workspace_id`, `owner_user_id`, `visibility` (private\|workspace), name, description, system prompt, model, effort, `ground_in_brain`, `anthropic_agent_id`, `anthropic_agent_version`, status | — |
| `agent_connectors` | `agent_id`, `name` (unique per agent, referenced by `mcp_toolset.mcp_server_name`), `mcp_server_url`, `provider` or null for custom, `tool_allowlist` jsonb | Any credential material |
| `agent_vaults` | `(workspace_id, user_id)` → `anthropic_vault_id` | — |
| `agent_credentials` | `user_id`, `provider`, `mcp_server_url`, `anthropic_credential_id`, display name, `connected_at` | **The tokens.** They transit once, at vault-create, and are never written to disk. |
| `agent_sessions` | `agent_id`, `user_id`, `anthropic_session_id`, title, status, `stop_reason`, `list_cost_cents`, token counts, timestamps | **The transcript.** Say so in the migration comment so the next engineer doesn't helpfully add it — and say there that this is *not* `agent_runs` (§4.5). |
| `agent_schedules` | `agent_id`, `user_id` (whose vault fires it), `anthropic_deployment_id`, cron expression, timezone, prompt, status, `budget_cents` | Run records — read those from Anthropic on demand. |

RLS on `agent_definitions` is the one policy with real thought in it:

```sql
workspace_id = current_workspace_id()
AND (visibility = 'workspace' OR owner_user_id = current_user_id())
```

Everything else scopes through the parent.

### 5.2 Module layout

```
brain-api/app/modules/agents/
  router.py             # paths + deps only
  service.py            # agent CRUD, Anthropic sync, session lifecycle
  repository.py         # SQL, stateless, session-first
  schemas.py            # CamelModel out / CamelRequestModel in
  anthropic_client.py   # thin wrapper: agents, sessions, vaults, deployments
  catalog.py            # reads agent_connectors.yaml, like source_authority.yaml
  credentials.py        # per-provider OAuth → vault, never persists a token
  stream.py             # httpx.stream → StreamingResponse, zero buffering
```

### 5.3 Endpoints

| Route | Does |
|---|---|
| `GET /agents` | List agents visible to the caller — own private plus workspace-published. |
| `POST /agents` | Create the Anthropic agent, store `agent_id` + `version`. |
| `PATCH /agents/{id}` | Update Anthropic (new version), bump stored version. Pass `version` for a 409 on concurrent edits. |
| `POST /agents/{id}/publish` | Flip visibility to workspace. Unpublish is the inverse. |
| `GET /agents/{id}/versions` | Proxy the agent's version history. |
| `GET /agents/catalog` | Curated connector catalog from YAML. |
| `GET POST DELETE /agents/{id}/connectors` | Declare which MCP servers this agent talks to. |
| `GET /agent-credentials` | What the calling user has connected, and which agents need something they lack. |
| `POST /agent-credentials/{provider}/authorize` | Returns the consent URL. Same contract as `/sources/{provider}/authorize`. |
| `GET /agent-credentials/{provider}/callback` | Exchange, find-or-create vault, POST credential to Anthropic, redirect. **Token never written to Postgres.** |
| `POST /agents/{id}/sessions` | Create a session with the caller's vault attached and the first message as `initial_events`. |
| `GET /agent-sessions/{id}/stream` | SSE pass-through of Anthropic's event stream. |
| `GET /agent-sessions/{id}/events` | Replay proxy for reconnect. Proxied, not stored. |
| `POST /agent-sessions/{id}/events` | Send `user.message`, `user.interrupt`, `user.tool_confirmation`. |
| `GET POST PATCH DELETE /agents/{id}/schedules` | Deployment CRUD, plus pause and unpause. |
| `POST /agents/{id}/schedules/{sid}/run` | Manual run. Works even while paused — this is the "Run now" button. |
| `GET /agents/{id}/schedules/{sid}/runs` | Proxy `deployment_runs`, 30s Redis cache. Failures included. |
| `POST /webhooks/anthropic` | HMAC-verified. Updates session status and usage only — never content. |

### 5.4 Two vaults per session, not one

A vault holds at most **20 credentials**, keyed uniquely by MCP server URL. If
`query_brain`'s bearer credential lives in the user's vault it eats one of those
slots for every user. Instead keep a **workspace vault** holding just the
`query_brain` credential and attach both:

```python
vault_ids=[user_vault_id, workspace_vault_id]
```

Users keep all 20 connector slots, and rotating the Brain's key is one write
instead of N.

### 5.5 Enforcing the wall in CI, not in a doc

- An AST test that fails if `app/modules/agents/**` imports `app.pipeline.*`, or
  the reverse. **Exception, written as an allowlist rather than a hole:** when
  §4.6's metadata tap lands, the envelope builder *may read event metadata, never
  event content*. Encode that as the specific symbols it may touch, so the guard
  gets sharper, not weaker.
- A schema test asserting `agent_sessions` has no text column beyond `title` and
  `stop_reason`. This holds under §4.6 — the tap stores nothing new here.
- Streaming is `httpx.AsyncClient.stream` straight into `StreamingResponse`. If a
  reviewer sees an accumulator variable in `stream.py`, that's the bug.
- Cross-tenant negative tests and auth-failure tests on every new route, per the
  existing convention.

### 5.6 Cost control

Set a session budget on **every** session:

```python
budget={"type": "limit", "max_list_cost": {"amount": "200", "currency": "USD"}}
```

`"200"` is $2.00 — minor units, integer string, no decimals. A session at its cap
pauses `idle` with `stop_reason: budget_reached` rather than dying; raising the
cap resumes it. Do the same on deployments, where the cap is copied onto each
fired session. Without this, one user's runaway scheduled agent is our invoice.

---

## 6. Frontend

One feature slice in the existing convention. Three screens, one hard part.

```
brainites-fe/src/features/agents/
  api/        agents.api.ts · agents.schemas.ts
  hooks/      useAgents · useSaveAgent · useAgentConnectors · useAgentCredentials
              useAgentStream · useAgentSchedules · useScheduleRuns
  components/ AgentCard · AgentBuilderForm · ConnectorPicker · CustomMcpDialog
              GroundingToggle · AgentRunView · ToolCallBlock
              ToolConfirmationPrompt · ScheduleDialog · RunHistoryTable
  pages/      AgentsPage · AgentBuilderPage · AgentRunPage
```

Routes go under `DashboardLayout` in `src/app/router/index.tsx`:
`/dashboard/agents`, `/dashboard/agents/new`, `/dashboard/agents/:id`,
`/dashboard/agents/:id/edit`. The credential callback needs the same trick
`SourcesCallbackRedirect` already pulls — the backend redirects to a path React
Router doesn't serve, so forward it with the query string intact.

### 6.1 The three screens

- **Agents** — cards in two groups, *Yours* and *Shared in {workspace}*. Each card
  shows connector avatars, schedule state, last run. Reuses the `SourceCard`
  shape almost verbatim.
- **Builder** — name, description, system prompt, model + effort, session budget,
  a grounding toggle for `query_brain`, and the connector picker. The picker is
  the catalog grid plus one row at the bottom: *Connect any MCP server* → URL
  field. A connector the user hasn't authorized renders as *Needs your Slack
  account* with a Connect button that does a full-page redirect, exactly like
  `useConnectSource`.
- **Run** — two tabs, *Chat* and *Runs*. Chat is the streamed transcript; Runs is
  the deployment-run table with failures shown, since a scheduled agent that
  silently stops running is the failure mode people actually hit.

### 6.2 The hard part: the event reducer

`useBrainChat`'s SSE handling doesn't carry over — the Brain streams tokens, an
agent session streams a typed event union. `useAgentStream` reduces
`agent.message`, `agent.thinking`, `agent.tool_use` / `tool_result`,
`agent.mcp_tool_use` / `mcp_tool_result`, `session.status_*`, `session.usage` and
`session.error` into a render list.

Three details that will otherwise cost a day each:

| Trap | What happens | Do this |
|---|---|---|
| Reconnect drops events | SSE has no replay. Reopening the stream starts from "now" and silently loses everything in between. | Open the stream **first**, then fetch history via the events list, dedupe on event id as the live stream catches up. Terminal checks must run even for already-seen events, or the loop never exits. |
| The idle gate | Treating any `session.status_idle` as "done" freezes the UI when the agent is actually waiting for a tool confirmation. | Break only on `status_terminated`, or `status_idle` whose `stop_reason.type` is not `requires_action`. |
| Pending never clears | Sent events appear twice — once with `processed_at: null`, once populated. But tool-result events skip the queued phase entirely. | Treat a populated `processed_at` on first sighting as immediately acknowledged. |

Opt into live previews with `?event_deltas[]=agent.message` on the stream so text
renders as it generates — the buffered `agent.message` stays authoritative. Note
the delta type is `content_delta`, **not** the Messages-API `content_block_delta`;
accumulator code from elsewhere won't drop in.

---

## 7. Build order

Sequenced so backend never blocks on frontend after phase 1. Days are one
engineer per track.

| # | Phase | Track | Days |
|---|---|---|---|
| 0 | **Make `query_brain` reachable** — Streamable HTTP (switch, don't dual-mount — §4.1), bearer auth alongside `X-API-Key`, HTTPS ingress, and the `agent_origin` key flag defaulting on for every non-dashboard credential (§4.4). `plugin/.mcp.json` is written against the new mount path in the same pass, and marketplace submission is gated on this ingress. Nothing else can be tested end-to-end until this lands. | BE | 1 |
| 1 | **Migration and agent CRUD** — six tables with RLS, the agents module, the Anthropic client wrapper. Agents create lazily on first save and sync version on every update. The migration comment must distinguish `agent_sessions` from `agent_runs` (§4.5). Ships with cross-tenant tests. Unblocks the FE contract. | BE | 3 |
| 2 | **Credentials and vaults** — the expensive phase, and the one with nothing to do with Claude. Four provider OAuth flows, find-or-create vault, credential POST with the `refresh` block wired to each provider's token endpoint. | BE | 4 |
| 3 | **Agents list and builder** — parallel with phase 2 against the phase-1 contract. List, builder form, connector picker, custom MCP dialog, publish toggle. | FE | 3 |
| 4 | **Sessions, stream proxy, webhook** — session create with dual vaults and a budget, the pass-through SSE proxy, send-events, the HMAC webhook that keeps status and usage current without polling. Write `stream.py` knowing §4.6's metadata tap is coming: no accumulator, but a seam where event *metadata* can be observed. | BE | 3 |
| 5 | **Run view and event reducer** — transcript, tool-call blocks, confirmation prompt, and the three reconnect traps above. Largest FE piece. | FE | 3 |
| 6 | **Schedules and runs** — deployment CRUD mapped onto a cron picker, pause/unpause, Run now, run history with failures visible. Cheap because the scheduler isn't ours. | BE 2 · FE 2 | 4 |
| 7 | **Wall enforcement, docs, polish** — import-guard and schema tests, `make docs`, empty states, error copy, connector-not-authorized path end to end. | BE 2 · FE 1 | 3 |

| Track | Phases | Days |
|---|---|---|
| Backend | 0, 1, 2, 4, 6, 7 | 15 |
| Frontend | 3, 5, 6, 7 | 9 |
| **Critical path, two engineers parallel** | backend-bound after phase 1 | **≈16** |
| One engineer, sequential | everything | ≈24 |

Roughly **three working weeks with two people**, five solo. The original 1–2 day
guess is the phase-0 spike with one hardcoded agent — a real milestone, just not
the product.

---

## 8. Limits worth designing around

| Limit | Consequence |
|---|---|
| 20 credentials per vault | 20 connectors per user. The workspace-vault split (§5.4) keeps `query_brain` from eating one. |
| 20 MCP servers, 128 tools per agent | Not a real ceiling for v1, but cap the connector picker at 20 with a clear message rather than letting Anthropic reject the save. |
| Scheduled runs jitter up to 9 minutes | Never promise an exact fire time in the UI. Say "around 8:00 PM". |
| 1,000 deployments per organization | One per user schedule. At scale we'd need one deployment reused across users, or a support ticket. Fine for v1. |
| DST wall-clock matching | 2 AM schedules are skipped in spring and fire twice in autumn. Default the cron picker away from 1–3 AM. |
| Invalid vault credentials don't fail session creation | The session starts and emits a `session.error`. Surface that as "Your Slack connection expired", not as a dead run. |
| MCP tokens are not API keys | A Notion `ntn_` integration token authenticates Notion's REST API and will **not** work as an MCP vault credential. Different auth systems; the OAuth flow is mandatory. |
| Managed Agents is Anthropic-only | The agent runtime can't ride the `LLM_PROVIDER` factory. Two runtimes: the Brain stays swappable, agents don't. Worth saying out loud before it surprises someone in a sales call. |

---

## 9. If it has to ship in a week

In cut order. The first two cost almost nothing; the third starts to hurt.

1. **Drop schedules** (phase 6, 4 days). Chat-only agents are still a product.
   Deployments are additive later — no schema rework, because `agent_schedules`
   touches nothing else.
2. **Drop custom MCP URLs** (~1 day). Ship the curated catalog only. Arbitrary
   URLs raise support questions we don't have answers to yet.
3. **Cut to two providers** (~2 days). Slack and GitHub cover most demos. Each
   additional provider stays a day.
4. **Don't cut publishing.** Retrofitting `owner_user_id` and `visibility` into an
   RLS policy after there's data is far worse than the half-day it costs now.

---

## 10. Strategic caveat

A thin agent builder over Anthropic isn't defensible on its own — Anthropic can
undercut it, and probably will. Build this as *"an agent that already knows how
your company works"*: `query_brain` on by default, the grounding visible in the
transcript, connectors treated as commodity glue.

**Capture is not the moat — two competitors already capture more surface than we
do. The gate is.**

| Product | What it captures | Gate | Consent posture |
|---|---|---|---|
| Hyper | Prompts and outcomes. Three hooks, no `PostToolUse` — never the trajectory | None | Writes by default; mute as escape hatch |
| Mem0 | The fullest hook surface shipped, trajectory included | None — episodic memory, ungated and unreviewed | Global |
| Memory Store | Claude chats and Codex sessions as a first-class source, MCP-only | None | Ingestion-shaped |
| **Brainite** | Trajectory as digests, plus a first-party agent runtime | **Success gate, clustering, human review, versioned skills** | **Opt-in per repo** |

*(Competitor rows reflect what those clients were documented to ship, not
independent testing.)*

Mem0 cannot copy the gate without becoming a different product; Hyper will not
copy the consent posture without giving up its default-on capture. Neither has a
first-party agent runtime. If the builder feeds the loop (§4.6), the agents get
better the more they are used — a compounding story none of the three is
structured to tell. If it does not, we are an Anthropic agent builder with a wiki
attached, which this section already admits is not defensible.

The moat is the reviewed, versioned, trust-scored Brain. The agent builder is how
people reach it — and, once §4.6 lands, one of the things that fills it.
