# Golden hook payloads — captured, not assumed

Captured 2026-08-23 from a real Claude Code session (`claude -p` in a scratch repo
with all six events teed to `scripts/capture-payload.sh`). Every field the binary
reads is observed here. `*.ndjson` holds every occurrence; `*.json` holds the last.

Absolute paths have been rewritten (`/tmp/probe-repo`, `/Users/dev/`) — these ship
in a plugin intended for distribution, and the capture machine's home directory is
not part of the contract. Nothing else was touched: the field names, the nesting
and the per-tool response shapes are exactly as the platform sent them.

**These fixtures are the contract. Where they disagree with `docs/PLUGIN_BUILD.md`
or `docs/AGENT_HOOK_SHIM.md`, they win.** They disagree in six places.

## 1. A failed tool call emits `PreToolUse` and **no** `PostToolUse`

The single most consequential finding. Probed with `cat /definitely/not/a/real/file`
and `Read /definitely/not/a/real/file`: both produced a `PreToolUse` and no
`PostToolUse` at all (6 `PreToolUse`, 4 `PostToolUse`, the 2 unmatched
`tool_use_id`s being exactly the failures).

Consequences:

- **`PLUGIN_BUILD.md`'s `hooks.json` omits `PreToolUse` entirely.** As written, it
  captures *only successful* tool calls — failures leave no trace whatsoever.
- The outcome resolver's rank 5 ("any of the last 3 steps has `status: error`") and
  the server's `error_free_tail` gate would then be fed a trajectory in which error
  is unreachable. Every run would look clean.
- **So we hook `PreToolUse` and reconcile by `tool_use_id`.** `PreToolUse` opens a
  step and assigns its index; `PostToolUse` closes it `ok`. A step still open at
  segment close is `status: "error"` — the tool failed, was denied, or the session
  was interrupted. This is the only path to the error signal.

`AGENT_HOOK_SHIM.md` had this right (`PreToolUse` → "stamp `t_start`"); the
`PLUGIN_BUILD.md` hook config dropped it.

## 2. The result field is `tool_response`, not `tool_result`

`PLUGIN_BUILD.md`'s correction #1 ("the field is **`tool_result`**") is itself
wrong. The observed key on `PostToolUse` is **`tool_response`**, exactly as
`AGENT_HOOK_SHIM.md` originally said.

## 3. There is no `stop_reason` on `Stop`

Observed `Stop` keys: `background_tasks`, `cwd`, `effort`, `hook_event_name`,
`last_assistant_message`, `permission_mode`, `prompt_id`, `session_crons`,
`session_id`, `stop_hook_active`, `transcript_path`.

So the rule "`stop_reason == max_tokens` forces `ambiguous`" has no input and is
dropped. `stop_hook_active` is a re-entrancy guard (true when a Stop hook already
fired), not an outcome signal — read it only to avoid double-closing a segment.

`last_assistant_message` **is** present, so we never parse `transcript_path`. ✅

## 4. `SessionEnd`'s field is `reason`, not `session_end_reason`

## 5. `PreToolUse` carries no `agent_id`

Subagent attribution is unavailable at this layer; `Task:<subagent_type>` from
`tool_input` is all we get, as v1 already assumed.

## 6. `PostToolUse` carries `duration_ms` — `latencyMs` is free

No need to diff timestamps between the Pre and Post hooks.

## Confirmed as documented

- **`prompt_id` is present on `UserPromptSubmit`, `PreToolUse`, `PostToolUse`,
  `Stop` and `SessionEnd`.** Segmentation by `prompt_id` holds. ✅
- `SessionStart` carries `source` (`startup`/`resume`/`clear`/`compact`).
- `tool_response` is a **per-tool dict**, not a string:
  `Read` → `{file, type}` · `Bash` → `{stdout, stderr, interrupted, isImage,
  noOutputExpected}` · `Grep` → `{content, filenames, mode, numFiles, numLines,
  totalLines}` · `Write` → `{type, filePath, content, structuredPatch,
  originalFile, userModified}`. Digest the serialized form; never store it.
- Parallel tool calls **complete out of order** (`Grep` closed before `Bash` here
  despite starting after). Step index must be assigned at `PreToolUse` — arrival
  order — or the envelope's contiguous-execution-order validator gets a shuffled
  list.
