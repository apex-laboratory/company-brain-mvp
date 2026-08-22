# Building the Brainite plugin — implementation guide

How to build the client that feeds `POST /runs` and injects skills back. Companion
to `docs/AGENT_HOOK_SHIM.md` (which specs *what* the client does); this doc is the
*how*, against the platform contracts as verified **August 2026**.

> **This doc corrects `AGENT_HOOK_SHIM.md` in six places** — see
> [Corrections](#corrections-to-agent_hook_shimmd). The shim spec was written
> against assumed payload shapes; these are the real ones.

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
| `SessionStart` | `startup`, `resume`, `clear`, `compact`, `fork` | `session_id`, `transcript_path`, `cwd`, `model` | Open spool, emit brief |
| `UserPromptSubmit` | — | `session_id`, **`prompt_id`**, `prompt`, `cwd`, `permission_mode` | Segment + inject |
| `PreToolUse` | tool name regex | `tool_name`, `tool_input`, `tool_use_id`, `agent_id` | Stamp step start |
| `PostToolUse` | tool name regex | `tool_name`, `tool_input`, **`tool_result`**, `tool_use_id` | **The trajectory** |
| `Stop` | — | **`last_assistant_message`**, `stop_reason` | Close run, flush |
| `PreCompact` | `manual`, `auto` | `compaction_trigger` | Flush before context loss |
| `SessionEnd` | `clear`, `resume`, `logout`, `prompt_input_exit`, `other` | `session_end_reason` | Final drain |

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
  a public HTTPS endpoint. Ours already is (SSE, `X-API-Key`).
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
| `posttool` | `tool_name`, `tool_input`, `tool_result`, `tool_use_id` | Map to step envelope, redact, append to spool | nothing |
| `stop` | `last_assistant_message`, `stop_reason` | Close segment, resolve outcome, spawn detached flush | nothing |
| `flush` | — | Assemble + POST closed segments | nothing |
| `session-end` | `session_end_reason` | Signal detached flusher, return immediately | nothing |

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

`status` is `error` when `tool_result` indicates failure; `resultDigest` is
`sha256(tool_result)[:16]` plus `{bytes, lines}`. **Never spool `tool_result` content
itself** — only its digest and whatever survives redaction of `tool_input`.

> The `Stop` payload carries `last_assistant_message` directly, so **we never parse
> `transcript_path`.** That removes the version-drift risk the shim spec flagged.

---

## Outcome resolution

Unchanged from `AGENT_HOOK_SHIM.md` — six ranked signals, `ambiguous` as the default,
server owns the gate. Two implementation notes:

- `stop_reason` is on the `Stop` payload. `max_tokens` means the agent was cut off
  mid-task → force `ambiguous` regardless of other signals.
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
3. Submit `https://mcp.brainites.com/sse` as the production endpoint; verify the domain.
4. Strip "Claude" from all skill text; replace any `userConfig` with OAuth or explicit input.
5. Supply reviewer credentials (a demo workspace API key), 5 positive + 3 negative tests.
6. Ship the ChatGPT experience **assuming no hooks** — read-only `query_brain`, per
   OpenAI's own guidance.

---

## Build order

| # | Milestone | Proves |
|---|---|---|
| 1 | Binary skeleton: stdin parse, always-exit-0, spool append, `posttool` mapping | a trajectory lands on disk |
| 2 | `prompt` segmentation by `prompt_id`, `stop` outcome, detached flush → `POST /runs` | **a real trajectory reaches `agent_runs`** |
| 3 | `/brain-done` marker + skill | live demo: task → `/brain-done` → cluster ready |
| 4 | Injection + cache + provenance line | the loop closes visibly |
| 5 | Client redaction, `.brainignore`, `/brain mute`, `/brain status` | shippable to someone else's machine |
| 6 | Codex adapter (`turn_id`, `async: true`, trust-stable shim) | second surface, ~1 day |
| 7 | Claude marketplace + OpenAI directory submission | distribution |

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

| # | Shim spec said | Reality |
|---|---|---|
| 1 | `tool_response` | The field is **`tool_result`** |
| 2 | Segment index synthesised from session id | Use the platform's **`prompt_id`** (`turn_id` on Codex) |
| 3 | Parse `transcript_path` for the final message | **`last_assistant_message`** is on the `Stop` payload |
| 4 | `SessionEnd` does the final flush | **1.5s shared budget** — signal a detached flusher, nothing more |
| 5 | Codex is "v2, a different event adapter" | Codex has a **near-identical hook system**; it is milestone 6, ~1 day |
| 6 | Hook contract drift is a parse-failure risk | Also: **exit 2 on `UserPromptSubmit` erases the user's prompt** — never exit non-zero |

Everything else in that spec — the hot/flush split, the ambiguous-by-default outcome
resolver, opt-in per repo, the spool format, `min_runs_per_cluster` deferral to the
server — stands as written.
