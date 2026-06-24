#!/bin/bash
# Usage: ./scripts/oauth_exchange.sh "<full redirect URL from browser>"
URL="$1"
if [ -z "$URL" ]; then
  echo "Usage: $0 \"<redirect URL from browser>\""
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$REPO_ROOT/brain-api/.env"

python3 - "$URL" "$ENV_FILE" <<'EOF'
import sys, json, asyncio, urllib.parse
import httpx

url      = sys.argv[1]
env_file = sys.argv[2]

params = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
code   = params.get("code")
state  = params.get("state")

if not code or not state:
    print("ERROR: no code or state found in URL")
    sys.exit(1)

env = {}
with open(env_file) as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()

client_id     = env.get("GOOGLE_CLIENT_ID", "")
client_secret = env.get("GOOGLE_CLIENT_SECRET", "")
redirect_base = env.get("OAUTH_REDIRECT_BASE_URL", "http://localhost:4000")
redirect_uri  = f"{redirect_base}/api/v1/auth/oauth/google/callback"

print(f"client_id    : {client_id[:30]}...")
print(f"redirect_uri : {redirect_uri}")
print(f"code         : {code[:30]}...")
print()

async def run():
    async with httpx.AsyncClient(timeout=10) as client:
        print("=== API callback ===")
        api_resp = await client.post(
            "http://localhost:4000/api/v1/auth/oauth/google/callback",
            json={"code": code, "state": state},
        )
        print(json.dumps(api_resp.json(), indent=2))
        if api_resp.status_code >= 400:
            sys.exit(1)

asyncio.run(run())
EOF
