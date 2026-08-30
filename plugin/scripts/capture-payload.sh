#!/bin/sh
# Step 0 harness: tee one hook's stdin to testdata/golden/<event>.json.
#
# Every field name in PLUGIN_BUILD.md is asserted, not observed. This captures
# what the platform actually sends so the mapping is written against reality and
# drift becomes a failing golden test rather than silent data loss.
#
# Usage (from .claude/settings.json in a scratch repo):
#   "command": "/abs/path/capture-payload.sh PostToolUse"
set -eu
event="${1:?event name required}"
out="${BRAINITE_GOLDEN_DIR:?BRAINITE_GOLDEN_DIR required}"
mkdir -p "$out"
# One file per event, last write wins; PostToolUse gets every call appended to a
# .ndjson so we see the shape for several different tools, not just the first.
body=$(cat)
printf '%s\n' "$body" > "$out/$event.json"
printf '%s\n' "$body" >> "$out/$event.ndjson"
exit 0
