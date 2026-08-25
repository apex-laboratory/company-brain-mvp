# Building the Brainite plugin — implementation guide

How to build the client that feeds `POST /runs` and injects skills back. Companion
to `docs/AGENT_HOOK_SHIM.md` (which specs *what* the client does); this doc is the
*how*, against the platform contracts as verified **August 2026**.

 > **⚠️ The fixtures outrank this document.** Real hook payloads were captured
> from a live session in **August 2026** and live in `plugin/testdata/golden/`,
> with the differences catalogued in that directory's `README.md`. **They
> contradict this doc in seven places, and where they do, they win** — they were
> observed, this was asserted. Every correction is folded in below and marked
> **[fixture]**; the ones that changed the design are called out where they bite.
>
> This doc still corrects `AGENT_HOOK_SHIM.md` — see
> [Corrections](#corrections-to-agent_hook_shimmd) — but two of its own
> "corrections" were themselves wrong.

---

## The headline finding

**Codex is a first-class capture target, not a v2 afterthought.** OpenAI shipped a
hooks system in Codex CLI (v0.114, March 2026) whose `hooks.json` schema is
near-identical to Claude Code's — same event names, same matcher groups, same
stdin-JSON/stdout-JSON contract, same exit-code semantics. One binary serves both
with a thin payload adapter.

That collapses the three surfaces into two behaviours:

| Surface | Inject | Capture | Why |
|---|---|---|---|
| **Claude Code** | ✅ hooks | ✅ hooks | Full lifecycle |
| **Codex CLI** | ✅ hooks | ✅ hooks | Same hook model; `async: true` is a bonus |
| **ChatGPT** | ⚠️ MCP tools only | ❌ | **ChatGPT does not run plugin hooks.** Confirmed in OpenAI's own submission guide |

ChatGPT stays read-only. Everything else gets the full loop.

---

## Platform contracts (verified August 2026)

### Claude Code hooks

Config lives in `hooks/hooks.json` inside the plugin — **the schema is byte-identical
to `settings.json`**, so anything that works in one works in the other.

| Event | Matchers | Key stdin fields | Our use |
|---|---|---|---|
| `SessionStart` | `startup`, `resume`, `clear`, `compact`, `fork` | `session_id`, `transcript_path`, `cwd`, `source` **[fixture: `source`, not `model`]** | Open spool, emit brief |
| `UserPromptSubmit` | — | `session_id`, **`prompt_id`**, `prompt`, `cwd`, `permission_mode` | Segment + inject |
| `PreToolUse` | tool name regex | `tool_name`, `tool_input`, `tool_use_id`, `prompt_id` **[fixture: no `agent_id`]** | **Open the step — this is where failures are recorded** |
| `PostToolUse` | tool name regex | `tool_name`, `tool_input`, **`tool_response`**, `tool_use_id`, `duration_ms` **[fixture]** | **The trajectory** |
| `Stop` | — | **`last_assistant_message`**, `stop_hook_active` **[fixture: there is no `stop_reason`]** | Close run, flush |
| `PreCompact` | `manual`, `auto` | `compaction_trigger` | Flush before context loss |
| `SessionEnd` | `clear`, `resume`, `logout`, `prompt_input_exit`, `other` | `reason` **[fixture: not `session_end_reason`]** | Final drain |

> **`prompt_id` is present on `UserPromptSubmit`, `PreToolUse`, `PostToolUse`,
> `Stop` *and* `SessionEnd`** — confirmed against the fixtures. Segmentation by
> `prompt_id` holds exactly as specced below.

#### The finding that changes the hook set: a failed tool call emits no `PostToolUse`

Probed directly (`cat /definitely/not/a/real/file`, `Read` of a missing path):
both produced a `PreToolUse` and **no `PostToolUse` at all** — 6 `PreToolUse`
against 4 `PostToolUse`, the two unmatched `tool_use_id`s being exactly the
failures.

So `PostToolUse` alone captures **only successful tool calls**. The consequences
are not cosmetic:

- The outcome resolver's rank 5 ("an error in the last 3 steps") and the server's
  own `error_free_tail` check in `gate_run.evaluate` would both be fed a
  trajectory in which `error` is unreachable. **Every run would look clean.**
- Failures — the dead ends a *procedure* is partly made of — would leave no trace.

**Therefore `PreToolUse` is hooked, and the two are reconciled by `tool_use_id`:
`PreToolUse` opens a step and assigns its index; `PostToolUse` closes it `ok`; a
step still open at segment close is `status: "error"`.** That is the only path by
which a failure reaches the trace. `AGENT_HOOK_SHIM.md` had this right; the
`hooks.json` printed later in this doc originally omitted `PreToolUse`, and that
was a bug.

Also from the fixtures: **parallel tool calls complete out of order** (`Grep`
closed before `Bash` despite starting after). Step indices must be assigned at
`PreToolUse`, in arrival order — closing order yields a shuffled list, and the
envelope's contiguous-execution-order validator rejects it.

**Output contract — the parts that can hurt us:**

- Exit `0` = success. Exit `2` = **blocking**, and on `UserPromptSubmit` it *erases
  the user's prompt*. **Our hooks must never exit 2 under any circumstance**, including
  an unhandled exception. Wrap `main()` in a catch-all that exits 0.
- Any other exit code is a non-blocking error; stderr is shown to the model on some
  events. Keep stderr empty on the happy path.
- stdout starting with `{` is parsed as JSON, otherwise treated as plain text.
- Context injection: `{"hookSpecificOutput": {"hookEventName": "...", "additionalContext": "..."}}`.
  On `UserPromptSubmit`, plain-text stdout is *also* added as context — but emit the
  JSON form so the event name is explicit.
- **Timeouts**: `UserPromptSubmit` defaults to **30s** (lowered from 600);
  `PreToolUse`/`PostToolUse` 600s; **`SessionEnd` has a 1.5-second budget shared
  across all `SessionEnd` hooks.** That last one is a hard constraint — a network
  flush cannot live there.

### Codex CLI hooks

Config at `~/.codex/hooks.json`, `<repo>/.codex/hooks.json`, or inline `[hooks]` in
`config.toml`. Layers **merge** rather than override, and a plugin manifest can bundle
hooks too.

Events: `SessionStart`, `SessionEnd`, `UserPromptSubmit`, `PreToolUse`,
`PermissionRequest`, `PostToolUse`, `PreCompact`, `PostCompact`, `SubagentStart`,
`SubagentStop`, `Stop`.

Differences from Claude Code that the adapter must absorb:

- Turn identity is **`turn_id`**, not `prompt_id`.
- Only `"type": "command"` handlers execute. `prompt` and `agent` handler types parse
  but are skipped — irrelevant to us, we only use `command`.
- **`"async": true`** runs a hook in the background while Codex continues. Use it for
  every flush hook; it is strictly better than our detached-spawn workaround.
- `PreToolUse` **cannot inject context** on Codex (returns an error). Don't try.
- The `Stop` hook requires valid JSON on stdout — plain text is treated as a failure.
- **Trust model**: non-managed command hooks require user review, and Codex records
  trust against the hook script's *hash*. **Every plugin update re-triggers review.**
  Keep the hook entrypoint a stable thin shim that execs the real binary, so routine
  updates don't nag the user.

### ChatGPT / the OpenAI plugin directory

ChatGPT and Codex share **one universal plugin directory**; publish once, discoverable
in both. Submission specifics:

- OpenAI converts `.claude-plugin/plugin.json` → `.codex-plugin/plugin.json`. Skills
  (`SKILL.md`, scripts, assets) and a remote MCP server carry over unchanged.
- **stdio MCP servers and `.mcpb` bundles are not accepted** — the MCP server must be
  a public HTTPS endpoint. **[fixture-adjacent correction] Ours is not one yet.**
  `app/mcp/server.py` binds port 8001 as a compose-internal service with no ingress,
  so a public HTTPS host (`mcp.brainite…`) is a prerequisite, not a given. The agent
  builder's phase 0 delivers exactly this ingress — **submission is blocked on it**,
  and the two efforts should not build it twice.
- Must strip Claude-specific language ("Claude" → "the model") and replace `userConfig`
  with explicit inputs, OAuth, or hosted storage.
- Submission needs: production `/mcp` URL, domain verification, exact CSP domains,
  reviewer credentials, and **5 positive + 3 negative test cases**.
- Guidance is explicit: *don't require hooks for the core ChatGPT workflow.*

---

## Repo layout

```
brainite-plugin/
  .claude-plugin/
    plugin.json                # Claude Code manifest (OpenAI converts this)
    marketplace.json           # only in the marketplace repo
  hooks/
    hooks.json                 # Claude Code
    codex-hooks.json           # Codex (merged into ~/.codex/hooks.json at install)
    brainite-hook              # stable thin shim → execs bin/brainite-hook
  bin/
    brainite-hook              # the real binary (Go or Rust; single static file)
  skills/
    brain-done/SKILL.md
    brain-status/SKILL.md
    brain-mute/SKILL.md
  .mcp.json                    # query_brain + report_run
  README.md
```

**Why a compiled binary, not Node or Python:** `UserPromptSubmit` runs on every prompt
and `PostToolUse` on every tool call. A Node cold start (~80–120ms) or Python
(~150ms+) is a tax on every interaction; a static Go binary starts in ~3ms. Hyper
ships exactly this shape (`~/.hyper/bin/hyper-hook`), and it is the right call.

**Why the thin shim in `hooks/`:** Codex hashes the hook command for its trust
prompt. Point `hooks.json` at a shim that never changes, and update `bin/` freely.

---

## Manifests

**`.claude-plugin/plugin.json`**

```json
{
  "name": "brainite",
  "version": "0.1.0",
  "description": "Your company's reviewed operational knowledge, in the agent's context",
  "author": { "name": "Brainite", "url": "https://brainites.com" },
  "repository": "https://github.com/brainite/plugin",
  "license": "Apache-2.0",
  "keywords": ["knowledge", "memory", "procedures", "context"],
  "skills": ["./skills/brain-done", "./skills/brain-status", "./skills/brain-mute"],
  "mcpServers": "./.mcp.json"
}
```

**`hooks/hooks.json`** (Claude Code)

```json
{
  "hooks": {
    "SessionStart": [{
      "matcher": "startup|clear",
      "hooks": [{ "type": "command",
        "command": "\"${CLAUDE_PLUGIN_ROOT}/hooks/brainite-hook\" session-start",
        "timeout": 5 }]
    }],
    "UserPromptSubmit": [{
      "hooks": [{ "type": "command",
        "command": "\"${CLAUDE_PLUGIN_ROOT}/hooks/brainite-hook\" prompt",
        "timeout": 5 }]
    }],
    "PreToolUse": [{
      "matcher": ".*",
      "hooks": [{ "type": "command",
        "command": "\"${CLAUDE_PLUGIN_ROOT}/hooks/brainite-hook\" pretool",
        "timeout": 5 }]
    }],
    "PostToolUse": [{
      "matcher": ".*",
      "hooks": [{ "type": "command",
        "command": "\"${CLAUDE_PLUGIN_ROOT}/hooks/brainite-hook\" posttool",
        "timeout": 5 }]
    }],
    "Stop": [{
      "hooks": [{ "type": "command",
        "command": "\"${CLAUDE_PLUGIN_ROOT}/hooks/brainite-hook\" stop",
        "timeout": 5 }]
    }],
    "PreCompact": [{
      "hooks": [{ "type": "command",
        "command": "\"${CLAUDE_PLUGIN_ROOT}/hooks/brainite-hook\" flush",
        "timeout": 5 }]
    }],
    "SessionEnd": [{
      "hooks": [{ "type": "command",
        "command": "\"${CLAUDE_PLUGIN_ROOT}/hooks/brainite-hook\" session-end",
        "timeout": 1 }]
    }]
  }
}
```

`PreToolUse` is not optional — see the failed-call finding above. Without it the
plugin captures only the tool calls that succeeded.

Note the `SessionStart` matcher is **`startup|clear`** — deliberately *not*
`resume|compact|fork`. A resumed or forked session already has an open spool and its
run segments are mid-flight; re-opening would double-count them.

`SessionEnd` gets `timeout: 1` because the platform budget is 1.5s **shared across all
SessionEnd hooks from every installed plugin**. It must do nothing but signal a
detached flusher and return.

**`hooks/codex-hooks.json`** — same structure, plus async where it helps:

```json
{
  "hooks": {
    "PostToolUse": [{ "matcher": ".*", "hooks": [
      { "type": "command", "command": "~/.brainite/bin/brainite-hook posttool --codex",
        "async": true, "timeout": 5 }]}],
    "Stop": [{ "hooks": [
      { "type": "command", "command": "~/.brainite/bin/brainite-hook stop --codex",
        "async": true, "timeout": 10 }]}]
  }
}
```

**`.mcp.json`**

```json
{
  "mcpServers": {
    "brainite": {
      "type": "sse",
      "url": "https://mcp.brainites.com/sse",
      "headers": { "X-API-Key": "${BRAINITE_API_KEY}" }
    }
  }
}
```

> **⏳ This block changes when the agent builder's phase 0 lands.** That phase
> switches `app/mcp/server.py` from `transport="sse"` to `transport="http"`
> (Anthropic Managed Agents connects over Streamable HTTP), and the decision taken
> was **switch, don't dual-mount**: there is no installed base to protect while the
> plugin is unpublished, and two transports on the one internet-facing service
> means two auth paths and two rate-limit surfaces to get wrong. SSE is also the
> deprecated MCP transport — dual-mounting means migrating twice.
>
> When it lands: `type` becomes `"http"` and the URL takes the new mount path.
> Until then this file is correct and **must not ship to a marketplace** — see the
> build order.
>
> Note also that `report_run` is **not** in this block, though the shim spec
> assumed it: no such tool exists in `app/mcp/server.py`, which exposes
> `query_brain` only. Outcome rank 1 is therefore unreachable, and `/brain-done`
> (rank 2) carries v1.

---

## The binary

One executable, subcommand per hook. Every subcommand follows the same skeleton:

```
read stdin (JSON, single line) ──┐
                                 ├─→ never block, never exit non-zero
dispatch on subcommand ──────────┘
```

Non-negotiable rules:

1. **Always exit 0.** A panic must be recovered and swallowed. On `UserPromptSubmit`,
   exit 2 erases the user's prompt — that is a data-loss bug in someone's editor, and
   it would be ours.
2. **Never write to stderr on the happy path.** Several events surface stderr to the
   model, which would pollute the context we are trying to improve.
3. **Hot subcommands do no network I/O.** `posttool` appends a line and returns.
   Target <15ms wall clock.
4. **Emit JSON on stdout only when injecting.** Otherwise print nothing.

### Subcommand behaviour

| Subcommand | Reads | Does | Prints |
|---|---|---|---|
| `session-start` | `session_id`, `cwd` | Enabled-repo check; write spool header; read cached brief | `additionalContext` or nothing |
| `prompt` | `prompt_id`, `prompt`, `cwd` | Close prior segment, open new one keyed by `prompt_id`; query brain under deadline | `additionalContext` or nothing |
| `pretool` | `tool_name`, `tool_input`, `tool_use_id`, `prompt_id` | Map to step envelope, redact, **open** the step and assign its index | nothing |
| `posttool` | `tool_name`, `tool_response`, `tool_use_id`, `duration_ms` | **Close** the step: status, digest, latency | nothing |
| `stop` | `last_assistant_message`, `stop_hook_active` | Close segment, resolve outcome, spawn detached flush | nothing |
| `flush` | — | Assemble + POST closed segments | nothing |
| `session-end` | `reason` | Signal detached flusher, return immediately | nothing |

### Segmentation — use `prompt_id`

`AGENT_HOOK_SHIM.md` proposed synthesising a segment index from the session id. Don't:
**Claude Code supplies `prompt_id` on `UserPromptSubmit`, `PreToolUse`, `PostToolUse`
and `Stop`.** Every step already carries the id of the turn that caused it.

```
run segment  = all steps sharing one prompt_id
task         = the prompt text from that UserPromptSubmit
externalId   = "cc_" + session_id + "_" + prompt_id
```

This is strictly better than the earlier scheme: it is correct under interleaving,
survives out-of-order hook delivery, and makes the `externalId` idempotency key
genuinely stable across re-flushes. On Codex the same field is `turn_id`.

### Step mapping

| `tool_name` | Envelope `type` | `name` |
|---|---|---|
| `Read`, `NotebookRead` | `file_read` | path from `tool_input.file_path`, relative to `cwd` |
| `Write`, `Edit`, `NotebookEdit` | `file_write` | same |
| `Bash` | `shell` | `tool_input.command` |
| `Grep`, `Glob`, `WebFetch`, `WebSearch` | `tool_call` | tool name |
| `mcp__*` | `tool_call` | full name (**keep `mcp__brainite__query_brain`** — Feature 34 reinforcement needs it) |
| `Task` | `tool_call` | `Task:<subagent_type>` |
| — | `assistant_message` | from `last_assistant_message` on `Stop` |

`resultDigest` is `sha256(tool_response)[:16]` plus `{bytes, lines}`. **Never spool
`tool_response` content itself** — only its digest and whatever survives redaction of
`tool_input`.

`status` comes from two places, and the second is the one that matters:

- `error` when `tool_response` reports failure in its own payload — a non-empty
  `stderr`, `interrupted: true`. This catches the tool that returned successfully
  while reporting failure inside.
- `error` when the step was **opened and never closed**, because a hard failure
  emits no `PostToolUse` at all. This is the only signal for a tool that failed,
  was denied, or was interrupted.

`tool_response` is a **per-tool object**, not a string: `Read` → `{file, type}`,
`Bash` → `{stdout, stderr, interrupted, isImage, noOutputExpected}`, `Grep` →
`{content, filenames, mode, numFiles, numLines, totalLines}`, `Write` → `{type,
filePath, content, structuredPatch, originalFile, userModified}`. Digest the
serialised form.

> The `Stop` payload carries `last_assistant_message` directly — confirmed against
> the fixtures — so **we never parse `transcript_path`.** That removes the
> version-drift risk the shim spec flagged.

---

## Outcome resolution

Unchanged from `AGENT_HOOK_SHIM.md` — six ranked signals, `ambiguous` as the default,
server owns the gate. Two implementation notes:

- **[fixture] There is no `stop_reason` on the `Stop` payload** — the observed keys
  are `background_tasks`, `cwd`, `effort`, `hook_event_name`,
  `last_assistant_message`, `permission_mode`, `prompt_id`, `session_crons`,
  `session_id`, `stop_hook_active`, `transcript_path`. The "`max_tokens` forces
  `ambiguous`" rule has no input and is dropped. `stop_hook_active` is a
  re-entrancy guard (true when a Stop hook is already running), not an outcome
  signal — read it only to avoid closing a segment twice.
- Rank 1 ("the model called `report_run`") is unreachable: no such MCP tool exists.
  Ranks 2–6 carry v1.
- `/brain-done` writes a marker file the next `stop` reads, because a slash command
  and a hook are separate processes.

## Injection

`prompt` subcommand, hard budget **800ms** against a platform timeout of 30s — the
platform's generosity is not permission to use it. On timeout, print nothing and exit
0. Cache keyed on `sha256(prompt)`, 5-minute TTL. Skip prompts under 25 chars or
matching continuation patterns (`continue`, `yes`, `go on`).

Output:

```json
{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit",
  "additionalContext":"[Brainite] Refund Handling v3 (confidence 0.94)\n…"}}
```

Always carry the provenance line. Silent injection is a trust liability.

---

## Install and auth flow

```
/plugin marketplace add brainite/plugin
/plugin install brainite
  → post-install skill prompts for the key
  → written to ~/.brainite/credentials (0600), never to .claude/settings.json
  → Codex: merge hooks/codex-hooks.json into ~/.codex/hooks.json
cd my-repo && /brain enable        # capture is opt-in per repo
```

`~/.brainite/config.json`: `enabled_repos[]`, `agent_name`, `inject.enabled`,
`inject.deadline_ms`, `test_commands[]`, `spool.max_mb`, `redact.extra_patterns[]`.

Key needs `runs:write` + `brain:query` (decision recorded in the Phase 7 commit).
`.claude/settings.json` is commonly committed — the credential must never land there.

---

## Distribution

**Claude Code:** a marketplace repo with `.claude-plugin/marketplace.json` listing the
plugin. `strict: true` if `plugin.json` owns component definitions.

**OpenAI plugin directory** (reaches ChatGPT *and* Codex, one submission):

1. Archive with `.claude-plugin/plugin.json` + at least one skill.
2. platform.openai.com/plugins → Create plugin → **With MCP**.
3. Submit the production MCP endpoint and verify the domain. **Confirm which
   transport the directory accepts before freezing the URL** — post-phase-0 the
   server speaks Streamable HTTP, and this doc's track record on unverified
   platform details is poor.
4. Strip "Claude" from all skill text; replace any `userConfig` with OAuth or explicit input.
5. Supply reviewer credentials (a demo workspace API key), 5 positive + 3 negative tests.
6. Ship the ChatGPT experience **assuming no hooks** — read-only `query_brain`, per
   OpenAI's own guidance.

---

## Build order

| # | Milestone | Proves | Status |
|---|---|---|---|
| 0 | **Capture real hook payloads before writing code** — tee all six events, diff against this doc | the contract is observed, not assumed | ✅ `plugin/testdata/golden/` |
| 1 | Binary skeleton: stdin parse, always-exit-0, spool append, `pretool`/`posttool` mapping | a trajectory lands on disk | ✅ |
| 2 | `prompt` segmentation by `prompt_id`, `stop` outcome, detached flush → `POST /runs` | **a real trajectory reaches `agent_runs`** | ✅ verified live |
| 3 | `/brain-done` marker + skill | task → `/brain-done` → cluster ready for distillation | ✅ verified live |
| 4 | Injection + cache + provenance line | the loop closes visibly | ✅ |
| 5 | Client redaction hardening, `.brainignore`, `/brain mute`, `/brain status` | shippable to someone else's machine | ⏳ |
| 6 | Codex adapter (`turn_id`, `async: true`, trust-stable shim) | second surface, ~1 day | ⏳ |
| 7 | Claude marketplace + OpenAI directory submission | distribution | ⏳ **blocked on the agent builder's phase 0** — see below |

Milestone 0 was not in the original plan and should have been. Writing the mapping
against this doc's asserted field names would have produced a plugin that captured
no failures, stored no results (`tool_result` does not exist, so the digest would
have been of `nil`), and crashed on a `stop_reason` that is never sent. One probe
session cost twenty minutes and turned all of that into fixtures.

**Milestone 7 is gated on the MCP server becoming publicly reachable over
Streamable HTTP** (the agent builder's phase 0). Publishing before that would
create the installed base whose absence is the entire reason the transport switch
is cheap.

## Test plan

- **Golden payloads**: capture real hook stdin for every event, replay in unit tests.
  Payload shapes drift; a fixture makes drift a failing test rather than silent loss.
- **Exit-code fuzz**: assert exit 0 on malformed stdin, empty stdin, unwritable spool,
  unreachable API, and a forced panic. This is the single highest-risk behaviour.
- **Latency budget**: assert p95 `posttool` < 15ms and `prompt` < 800ms in CI.
- **Leak test**: seeded secrets through the full path, assert nothing reaches the wire.
- **Round-trip**: hook fixtures → binary → `POST /runs` against a local stack → assert
  `agent_runs` row shape, then that `gate_run` clusters as expected.

---

## Corrections to `AGENT_HOOK_SHIM.md`

> **Two rows of this table were themselves wrong.** Rows 1 and 3 below are kept
> with their errors struck through, because the failure mode is the point: both
> were asserted confidently, neither was observed, and row 1 would have silently
> broken every result digest. The fixtures in `plugin/testdata/golden/` are now the
> authority for all of it.

| # | Shim spec said | Reality |
|---|---|---|
| 1 | `tool_response` | ~~The field is **`tool_result`**~~ — **the shim spec was right.** The observed key is **`tool_response`**. |
| 2 | Segment index synthesised from session id | Use the platform's **`prompt_id`** (`turn_id` on Codex) |
| 3 | Parse `transcript_path` for the final message | **`last_assistant_message`** is on the `Stop` payload ✅ confirmed — but the accompanying claim that `stop_reason` is there too was wrong; **there is no `stop_reason`** |
| 4 | `SessionEnd` does the final flush | **1.5s shared budget** — signal a detached flusher, nothing more |
| 5 | Codex is "v2, a different event adapter" | Codex has a **near-identical hook system**; it is milestone 6, ~1 day |
| 6 | Hook contract drift is a parse-failure risk | Also: **exit 2 on `UserPromptSubmit` erases the user's prompt** — never exit non-zero |

Everything else in that spec — the hot/flush split, the ambiguous-by-default outcome
resolver, opt-in per repo, the spool format, `min_runs_per_cluster` deferral to the
server — stands as written. So does its `PreToolUse` hook, which this doc wrongly
dropped.

## Corrections to *this* document

Found by capturing real payloads (`plugin/testdata/golden/README.md`) and by
running the client against a live stack.

| # | This doc said | Reality |
|---|---|---|
| 1 | `tool_result` on `PostToolUse` | **`tool_response`** |
| 2 | `hooks.json` needs no `PreToolUse` | **It does.** A failed tool call emits `PreToolUse` and *no* `PostToolUse`, so without it failures are invisible and every run looks clean |
| 3 | `stop_reason` on `Stop` | Not present. `stop_hook_active` is a re-entrancy guard, not an outcome signal |
| 4 | `session_end_reason` on `SessionEnd` | The key is **`reason`** |
| 5 | `agent_id` on `PreToolUse` | Not present |
| 6 | `model` on `SessionStart` | The key is **`source`**. `PostToolUse` also carries `duration_ms`, so `latencyMs` is free |
| 7 | "the MCP server must be a public HTTPS endpoint. Ours already is" | It is not. Port 8001 is compose-internal with no ingress; that ingress is the agent builder's phase 0 |

One more, found only by running it end-to-end rather than by reading: `gate_run`
died on every real ingest with `ValueError: could not convert string to float: '['`.
No pgvector codec is registered, so `task_embedding` comes back as its text literal
and `list()` split it into characters — **every run reached the gate and none was
ever clustered.** Unit tests could not catch it; they inject a mocked repository.
Fixed at the repository boundary in `app/modules/runs/repository.py`.
