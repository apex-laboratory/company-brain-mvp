# Brainite plugin

Captures what an agent actually did, and feeds the company's reviewed procedures
back into its context.

One binary. `PreToolUse`/`PostToolUse` append a step to a local journal;
`UserPromptSubmit` looks up a matching skill under an 800ms deadline; a detached
flusher pushes closed runs to `POST /runs`. Nothing on the agent's critical path
touches the network except that one deadlined lookup.

Server contract: `docs/PRD.md` Feature 29-30. Spec: `docs/AGENT_HOOK_SHIM.md`
(what) and `docs/PLUGIN_BUILD.md` (how). **Where those disagree with
`testdata/golden/README.md`, the fixtures win** — they were captured from a live
session and the specs were not.

## Install

```bash
make build            # → bin/brainite-hook
make install-local    # symlink into ~/.claude/plugins for dogfooding

mkdir -p ~/.brainite && chmod 700 ~/.brainite
printf '%s' 'bk_your_key' > ~/.brainite/credentials && chmod 600 ~/.brainite/credentials

cd your-repo && brainite-hook enable    # capture is opt-in, per repo
```

The key needs `runs:write` (push) and `brain:query` (inject). It goes in
`~/.brainite/credentials`, never `.claude/settings.json` — people commit that file.

`~/.brainite/config.json` holds `enabled_repos`, `agent_name`, `api_base_url`,
`min_steps`, `test_commands`, `inject.{enabled,deadline_ms}`, `spool.max_mb` and
`redact.extra_patterns`.

## What it records

| Claude Code tool | Step type | `name` |
|---|---|---|
| `Read`, `NotebookRead` | `file_read` | path, **relative to the repo** |
| `Write`, `Edit`, `NotebookEdit` | `file_write` | same |
| `Bash` | `shell` | the command, redacted |
| `Grep`, `Glob`, `WebFetch`, `WebSearch` | `tool_call` | tool name |
| `mcp__*` | `tool_call` | full name, `mcp__brainite__query_brain` included |
| `Task` | `tool_call` | `Task:<subagent_type>` |
| final message at `Stop` | `assistant_message` | — |

Tool *results* are never recorded — only `sha256(response)[:16]` plus a byte and
line count. Secrets are stripped by pattern before anything reaches disk, and
again server-side before storage.

## Outcome

Ranked, first match wins, **`ambiguous` by default**:

| Rank | Signal | Outcome |
|---|---|---|
| 1 | `/brain-done` | `success`, `humanConfirmed` |
| 2 | a configured test command ran, none failed | `success`, `testsPassed` + `inferred` |
| 3 | `git commit` succeeded, no failing test | `success`, `inferred` |
| 4 | an error in the last 3 steps | `failure` |
| 5 | anything else | `ambiguous` |

Ambiguous runs are **sent, not dropped**: the server owns the gate, and ineligible
runs are a tracked product signal. `/brain-done` is the one signal a person gives
directly, and it is the only one that bypasses the three-runs-per-cluster wait —
so it is worth asking for.

## Two findings that shaped the design

**A failed tool call emits `PreToolUse` and no `PostToolUse`.** So `PreToolUse` is
hooked (the spec's `hooks.json` omits it), a step opened and never closed is
recorded as `status: error`, and that is the *only* path by which a failure
reaches the trace. Without it every run in the corpus would look clean and the
gate's error-free-tail check would be defeated.

**Parallel tool calls complete out of order.** Step indices are assigned at
`PreToolUse`, in arrival order, because the envelope requires contiguous execution
order and a shuffled list distils into a procedure whose steps are wrong.

Both are asserted by tests; see `testdata/golden/README.md` for all six places the
platform disagrees with the written specs.

## Tests

```bash
make test       # everything; TestMain builds the binary so it is never stale
make golden     # replay real captured hook payloads
make latency    # p95 posttool < 15ms, injection deadline actually enforced
```

The suite is organised around the ways this thing could hurt someone:

- **`TestAlwaysExitsZero`** — 77 combinations of malformed stdin, unwritable
  spool, unreachable API and forced panic across every subcommand. On
  `UserPromptSubmit`, exit code 2 *erases the user's prompt*; that must never
  happen because a spool file was read-only.
- **`TestSeededSecretsNeverSurvive`** — one credential of every shape we claim to
  catch, fed through the redactor.
- **`TestGoldenNeverStoresToolResponse`** — asserts against the real captured
  responses that no result content reaches the spool.
- **`TestLatencyPromptDeadline`** — points the hook at a server that never
  answers and asserts it returns in ~800ms, not the platform's 30s.
- **`brain-api/app/tests/runs/test_plugin_contract.py`** — validates this
  client's real output against the server's pydantic schema, so the two languages
  cannot drift apart silently.

## Status

Milestones 1-4 of `docs/PLUGIN_BUILD.md`. Not yet built: `.brainignore`,
`/brain mute`, `/brain status` (m5), the Codex adapter (m6), marketplace
submission (m7).

Note that nothing distils yet: `gate_run.py`'s Feature 31/32 seam is still a log
line, so a confirmed run reaches "cluster ready for distillation" and stops there.
