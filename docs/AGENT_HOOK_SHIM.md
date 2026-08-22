# Agent Hook Shim — capturing Claude Code runs into `POST /runs`

Client-side spec for the piece that makes **Phase 7 / Features 29–34** actually
receive traffic. `POST /runs` is specced (PRD §Feature 29) but nothing produces
envelopes for it. This doc specs the producer: a **Claude Code plugin** that turns
a coding session into normalized run envelopes and, on the way in, injects matched
skills into the agent's context.

Backend-only repo, but this artifact is a client. It ships from this repo because
it is defined entirely by our API contract; judge it by the envelope it emits.

---

## TL;DR

| | |
|---|---|
| **What** | A Claude Code plugin: 5 hooks + an MCP server + 2 slash commands |
| **Emits** | The Feature 29 normalized trace envelope → `POST /runs` (`X-API-Key`, `runs:write`) |
| **Injects** | `query_brain` results at `UserPromptSubmit`; a cached brief at `SessionStart` |
| **Never** | Blocks the agent. Hot hooks do zero network I/O — append to a local spool, flush out-of-band |
| **Server deltas** | `runs:write` scope, `POST /runs`, `file_write` step type — all currently unbuilt |
| **Cost to build** | ~600 LOC client + the Feature 29 endpoint |

---

## Why this, and why now

Both direct competitors already ship session capture; we ship an endpoint nobody
calls. From their shipped clients:

- **Hyper** (`heyhyper.ai`) hooks `SessionStart` → `hyper-hook brief`,
  `UserPromptSubmit` → `hyper-hook prompt`, `Stop` → `hyper-hook observe`. Three
  hooks, all POSTing to their API. **No `PostToolUse`, no `PreCompact`** — they
  capture prompts and outcomes, never the trajectory.
- **Mem0**'s Claude Code plugin hooks `SessionStart`, `UserPromptSubmit`,
  `PreToolUse` ×3, `PostToolUse`, `Stop`, `PreCompact` — the fullest capture surface
  shipped, but it stores episodic memory, ungated and unreviewed.
- **Memory Store** ingests "Claude chats and Codex sessions" as a first-class source,
  MCP-only, no hooks.

The differentiated capture is **the trajectory plus a success gate** — the tool
calls, file reads, shell commands and dead ends that a *procedure* is made of, with
runs that didn't demonstrably succeed thrown away. That is Features 30–32. This shim
is the only thing standing between that design and having any input.

**Design consequence:** `PostToolUse` is the hook that matters most and is exactly
the one Hyper skipped. If we ship only prompt/outcome capture we have built a worse
Hyper. The spine of this spec is the step stream.

---

## Scope boundary

**In scope (this doc):** the client — hooks, spool, segmentation, outcome
resolution, redaction pass 1, packaging, injection.

**Out of scope:** everything downstream of the HTTP request. Success gate (F30),
clustering (F30), trajectory compression (F31), procedure extraction (F32), review
card (F33) are the existing spec and unchanged.

**Server work this depends on, none of which exists yet:**

| Delta | Where | Note |
|---|---|---|
| `runs:write` scope | `app/modules/api_keys/schemas.py::ApiKeyScope` | currently `brain:query \| skills:invoke \| sources:read \| decisions:read` |
| `POST /runs` | new `app/modules/runs/` | router → service → repository, `202 {runId}` |
| `file_write` step type | Feature 29 envelope enum | see [Step mapping](#step-mapping) — PRD delta |
| `agent_runs` table | migration | PRD §9 already defines the columns |

The shim can be built and tested against a stub that returns `202` before any of
that lands.

---

## Architecture — two paths, one rule

**The rule: a hook on the agent's critical path never touches the network.**
Feature 29 already commits to this server-side ("ingestion never blocks the agent's
critical path"); the client must honour it too, or we tax every tool call in the
session. Hyper budgets 5s on `UserPromptSubmit`; a 5s stall per prompt is a product
defect, not a budget.

```
HOT PATH (sync, no network, <15ms)          FLUSH PATH (async, detached)
─────────────────────────────────           ────────────────────────────
UserPromptSubmit ─┐                          Stop ──────┐
PreToolUse ───────┤                          PreCompact ┤
PostToolUse ──────┼─→ append NDJSON  ───→    SessionEnd ┴─→ assemble envelope
                  │   ~/.brainite/spool/                   redact · cap · POST /runs
                  │   <session_id>.ndjson                  on failure: leave in spool
                  ┘                                        retry on next flush
```

The one deliberate exception is **injection** (`UserPromptSubmit` → `query_brain`),
which does need a round-trip. It gets a hard 800ms deadline and returns nothing on
timeout — see [Injection](#injection).

---

## Hook map

| Hook | Network? | Budget | Job |
|---|---|---|---|
| `SessionStart` | cached read | 1500ms | Emit a cached brief; open a spool file; write session header |
| `UserPromptSubmit` | yes, deadlined | 800ms | **Segment**: close the previous run, open a new one. Append `prompt` step. Query the brain, inject the match |
| `PreToolUse` | no | 15ms | Record `tool_name` + redacted `tool_input`, stamp `t_start` |
| `PostToolUse` | no | 15ms | Close the step: `status`, `latencyMs`, `resultDigest`. **The trajectory lives here** |
| `PreCompact` | detached | — | Flush now — context is about to be lost and `Stop` may never see it |
| `Stop` | detached | — | Close the open run, resolve outcome, flush |
| `SessionEnd` | detached | — | Final flush, close spool, drain retry queue |

Not used: `Notification`, `SubagentStop` (v2 — subagent traces are their own runs and
need a parent link the envelope has no field for).

> Hook payload field names (`session_id`, `transcript_path`, `cwd`, `tool_name`,
> `tool_input`, `tool_response`, `prompt`, `source`, `trigger`, `reason`) track the
> Claude Code hook contract and are version-sensitive. Pin a probed schema version in
> the spool header so a payload change is a detectable parse failure, not silent data
> loss.

---

## Run segmentation — a session is not a run

The envelope's unit is a **task**. A Claude Code session is a sequence of tasks with
no explicit boundary. The segmentation rule:

> **A run opens at `UserPromptSubmit` and closes at the next `UserPromptSubmit`,
> `Stop`, or `SessionEnd` — whichever comes first. `task` = the prompt text.**

Refinement turns ("no, use the other table") therefore produce several short runs for
one logical task. **Do not try to merge them client-side.** The server already
clusters on `cosine ≥ 0.85` over `task_embedding` scoped to `agent_name` (F30);
refinement prompts land in the same cluster by construction, and
`min_runs_per_cluster = 3` means the cluster — not the segment — is the distillation
unit. Client-side merging would be a second, worse clustering heuristic fighting the
first.

Two guards:

- **Drop trivial segments.** A segment with `< min_steps` steps and no tool call is
  conversation, not a run. Discard client-side; don't spend a request to be rejected.
- **Cap before POST.** 400 steps / 1 MB (F29). On overflow, split the run at a step
  boundary and emit `part 1..n` sharing an `externalId` prefix, rather than eating a
  `413`.

`externalId` = `cc_<session_id>_<segment_index>` — stable, idempotent on retry, and
lets the server dedupe a re-flushed spool file.

## Step mapping

| Claude Code tool | Envelope `type` | `name` | Notes |
|---|---|---|---|
| `Read`, `NotebookRead` | `file_read` | file path | path relative to `cwd`; absolute paths leak usernames |
| `Write`, `Edit`, `NotebookEdit` | **`file_write`** | file path | **not in the current enum — PRD delta.** Mapping writes to `tool_call` erases the read/write distinction, which is exactly the signal a procedure needs |
| `Bash` | `shell` | the command | most valuable and most dangerous — see redaction |
| `Grep`, `Glob`, `WebFetch`, `WebSearch` | `tool_call` | tool name | `args` redacted |
| `mcp__*` | `tool_call` | full tool name | includes our own `query_brain` — F34 reinforcement depends on seeing it |
| `Task` (subagent) | `tool_call` | `Task:<subagent_type>` | v1 records the call, not the child trace |
| assistant text at `Stop` | `assistant_message` | — | last message only; from `transcript_path` |

`resultDigest`: `sha256(result)[:16]` plus `{ bytes, lines, exitCode }`. Results over
2 KB are digest-only, matching the server's own rule (F29 step 1) — no reason to ship
what will be discarded.

---

## Outcome resolution — the hard part

Feature 30 gates on `outcome == success`. **Claude Code emits no success signal.**
Getting this wrong in the optimistic direction poisons the corpus with procedures
distilled from runs that failed; the review queue would catch it, but only by
spending a reviewer's attention on garbage.

Resolve in priority order, first match wins:

| Rank | Signal | Outcome | `outcomeSignals` |
|---|---|---|---|
| 1 | Model called the `report_run` MCP tool | as reported | `humanConfirmed: false` |
| 2 | User ran `/brain-done` | `success` | `humanConfirmed: true` → distills immediately (F30) |
| 3 | Test command in the segment exited 0, none exited non-zero | `success` | `testsPassed: true, inferred: true` |
| 4 | `git commit` succeeded in the segment | `success` | `inferred: true` |
| 5 | Any of the last 3 steps has `status: error` | `failure` | `inferred: true` |
| 6 | Anything else | **`ambiguous`** | `inferred: true` |

**`ambiguous` is emitted, not dropped.** Two reasons: the server owns the gate
(F30 excludes ambiguous already, and centralising that judgement keeps one
implementation), and ineligible runs are a tracked product signal (PRD §15 —
failure rates per agent). The client's job is honest labelling, not eligibility.

Rank 2 is the highest-value signal in the table and the cheapest to build: one slash
command, `humanConfirmed: true`, immediate distillation, no `min_runs_per_cluster`
wait. **`/brain-done` should ship in the first milestone** — it is the difference
between a demo that distils a procedure live and one that waits for three runs.

Test-command detection (rank 3) is a regex over the repo's known test invocations
(`make test`, `pytest`, `npm test`, …), configurable per project. Keep the list
explicit; inferring "was that a test?" from arbitrary shell is not worth an LLM call
on the client.

---

## Injection

Two surfaces, different jobs:

**`UserPromptSubmit` → `query_brain(situation=prompt)`.** The real product moment:
the agent gets the matched skill *before* it starts working. Constraints:

- Hard 800ms deadline, `additionalContext` on success, silence on timeout. Never
  surface an error into the agent's context — a failed lookup must be invisible.
- Skip when the prompt is short (`< 25` chars) or matches a no-op pattern
  (`continue`, `yes`, `go on`) — refinement turns don't need a re-query and would
  triple our request volume.
- Local LRU keyed on `sha256(prompt)`, 5-minute TTL, mirroring the server-side read
  cache (Phase 5) so a repeated prompt costs nothing.
- Injected text carries a visible provenance line (`skill · vN · confidence`) so the
  developer can see what the brain claimed. Silent injection is how Hyper reads and
  is a trust liability, not a feature.

**`SessionStart` → cached brief.** Cheap orientation: top-N published skills for this
`agent_name`/repo, served from a local cache refreshed opportunistically on flush.
1500ms budget, empty on miss. This is the least important half — resist growing it
into a second context dump.

---

## Spool format

`~/.brainite/spool/<session_id>.ndjson`, one JSON object per line, append-only,
`O_APPEND` so concurrent hooks can't interleave partial writes.

```
{"v":1,"t":"session","sessionId":"…","cwd":"…","agentName":"claude-code","hookSchema":"2026-08","startedAt":"…"}
{"v":1,"t":"segment_open","index":0,"task":"…","at":"…"}
{"v":1,"t":"step","segment":0,"index":0,"type":"tool_call","name":"query_brain","status":"ok","latencyMs":240}
{"v":1,"t":"step","segment":0,"index":1,"type":"shell","name":"pytest -q","status":"ok","resultDigest":"…","latencyMs":8100}
{"v":1,"t":"segment_close","index":0,"outcome":"success","outcomeSignals":{"testsPassed":true,"inferred":true},"at":"…"}
```

Flush reads closed segments, assembles envelopes, POSTs, and **only then** marks them
sent (a `.sent` sidecar offset). A crash mid-flush re-sends; `externalId` makes that
idempotent. Files with all segments sent are deleted after 24h.

Retry: 3 attempts, exponential backoff, then leave in spool for the next session's
flush. Offline for a week → the spool drains when connectivity returns. Cap the spool
at 50 MB and drop oldest-first with a logged warning; unbounded local growth on a
developer machine is a support ticket.

---

## Redaction and consent

Server-side redaction (F29 step 1) is authoritative. The client still runs a **first
pass**, because the trace leaves the customer's machine at this point and defence in
depth is the whole argument for being trusted with `Bash` output.

Client pass strips, by pattern, before anything reaches the spool: auth headers,
`sk-`/`ghp_`/`AKIA`-style tokens, `.env` values, connection strings, `Authorization:`
lines, and anything matching the repo's `.gitignore`d paths. **A step whose
redaction pass errors is dropped, not spooled** — same posture as the server ("never
stores the payload just in case").

Consent posture, and this is a deliberate divergence from the competition:

- **Opt-in per project**, not global-on. Hyper writes by default and offers `mute` as
  an escape hatch. Reading every prompt and every shell command in a repo by default
  is a posture we should not copy — and "we don't capture until you say so" is a
  sales line, not a limitation.
- `.brainignore` — glob patterns whose file steps are never recorded.
- `/brain mute` — session-level pause, mirroring the affordance developers now expect.
- `/brain status` — what was captured this session, what's spooled, what was sent.
  Auditability at the client is the same argument as the review queue at the server.

---

## Auth and config

`X-API-Key` from `~/.brainite/credentials` (chmod 600) or `$BRAINITE_API_KEY`. Never
in a repo-committed settings file — hooks live in `.claude/settings.json`, which
people commit.

**Open decision:** the shim needs `runs:write` (push) *and* `brain:query` (inject).
F29 defines `runs:write` as write-only precisely so a CI harness can push traces
without read access. Options: (a) one key with both scopes — simplest, and the trust
boundary is the same developer machine either way; (b) two keys — preserves the
write-only isolation and lets CI push with a key that cannot read the brain.
**Recommend (a) for the shim with the scopes kept separable**, so (b) remains
available for harness integrations without a redesign.

`~/.brainite/config.json`: `enabled_repos[]`, `agent_name`, `min_steps`,
`test_commands[]`, `inject.enabled`, `inject.deadline_ms`, `spool.max_mb`,
`redact.extra_patterns[]`.

---

## Packaging

A Claude Code plugin, mirroring how `gleanwork/claude-plugins` distributes Glean:

```
brainite-claude/
  .claude-plugin/plugin.json
  hooks/hooks.json            → 5 hooks, all shelling to one binary
  bin/brainite-hook           → single static binary, subcommands per hook
  skills/brain-done/SKILL.md  → /brain-done
  skills/brain-status/SKILL.md
  .mcp.json                   → query_brain + report_run
```

One binary, subcommand per hook (`brainite-hook prompt|pretool|posttool|stop|…`) —
Hyper's shape, and it is the right one: a single fast-starting binary keeps the hot
path honest where a Python entrypoint would not.

Codex support is the same spool and the same envelope behind a different event
adapter; design the spool writer so the hook layer is the only Claude-specific part.

---

## Build order

| # | Milestone | Proves |
|---|---|---|
| 1 | `runs:write` scope + `POST /runs` accepting and persisting envelopes (`202`, no gating) | the contract |
| 2 | Hooks + spool + flush, `PostToolUse` trajectory, `/brain-done` | **a real trajectory lands in `agent_runs`** |
| 3 | Outcome resolver ranks 3–6, segmentation guards, caps/splitting | honest labels at volume |
| 4 | `UserPromptSubmit` injection + cache + provenance line | the loop closes visibly |
| 5 | Redaction pass, `.brainignore`, `/brain mute`, `/brain status` | shippable to someone else's machine |
| 6 | Plugin packaging + marketplace | distribution |

Milestone 2 is the demo: run a task, `/brain-done`, watch a procedure card appear in
the review queue. That is the whole Phase 7 thesis in one screen recording, and it is
reachable well before F31/F32 are built — a raw trajectory in the review queue is
already more than either competitor shows.

---

## Failure modes

| Risk | Guard |
|---|---|
| Hook contract drift breaks parsing | Pinned `hookSchema` in the spool header; parse failure logs loudly, never silently drops |
| Injection latency taxes every prompt | 800ms deadline, LRU cache, short-prompt skip; measure p95 and alarm |
| Trace contains a secret | Two redaction passes, drop-on-error, `.brainignore`, opt-in per repo |
| Spool grows unbounded offline | 50 MB cap, oldest-first drop, warning surfaced in `/brain status` |
| Optimistic outcome labels poison the corpus | `ambiguous` default; only ranks 1–4 assert success; nothing auto-publishes (F32) |
| One session floods the queue | Trivial-segment drop, `min_runs_per_cluster`, `max_distillations_per_day` |
