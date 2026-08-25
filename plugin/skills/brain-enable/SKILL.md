---
name: brain-enable
description: Turn Brainite capture on for the current repository, or explain how capture and consent work. Use when the user says "/brain enable", "enable brainite", "start capturing here", "turn on brain capture", or asks whether this repo is being captured.
---

# Enable capture for this repo

Brainite captures **nothing** until a repo is opted in. To enable the current one:

```bash
brainite-hook enable
```

(If that is not on PATH, use `"${CLAUDE_PLUGIN_ROOT}/hooks/brainite-hook" enable`.)

This appends the repo's absolute path to `enabled_repos` in
`~/.brainite/config.json`. Nothing else changes and no data is sent by enabling.

## What gets captured once enabled

- The prompt that opened each task, and the assistant's final message.
- One step per tool call: its type (`file_read`, `file_write`, `shell`,
  `tool_call`), the file path *relative to the repo*, or the shell command.
- A **digest** of each tool's result — a hash plus a byte and line count. File
  contents and command output are never recorded.
- Secrets are stripped by pattern before anything is written to disk, and again
  server-side before storage.

## What the user should know

Capture is per-repo and reversible: remove the path from `enabled_repos` and it
stops. A credential is required for anything to be uploaded — without one, traces
accumulate locally and go nowhere. If the user asks about a repo with client work
or regulated data in it, say plainly that shell commands and file paths from that
repo would be recorded, and let them decide.
