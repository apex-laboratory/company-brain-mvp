#!/usr/bin/env bash

set -euo pipefail

AIRBYTE_PORT="${AIRBYTE_PORT:-8000}"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required but not installed." >&2
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "Docker is installed but not running. Start Docker Desktop and retry." >&2
  exit 1
fi

if ! command -v abctl >/dev/null 2>&1; then
  echo "abctl is required but not installed. Install it with Homebrew first." >&2
  exit 1
fi

abctl local install --port "${AIRBYTE_PORT}" --no-browser

echo
echo "Airbyte should be available at http://localhost:${AIRBYTE_PORT}"
echo "Fetch login credentials with: abctl local credentials"
