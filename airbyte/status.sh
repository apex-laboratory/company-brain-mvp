#!/usr/bin/env bash

set -euo pipefail

if ! command -v abctl >/dev/null 2>&1; then
  echo "abctl is required but not installed. Install it with Homebrew first." >&2
  exit 1
fi

abctl local status
