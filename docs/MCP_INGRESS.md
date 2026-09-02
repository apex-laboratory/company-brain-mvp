# Blocker: `query_brain` has no public HTTPS ingress

**Status:** open · **Blocks:** agent builder phase 4+, plugin marketplace submission
· **Owner:** unassigned (infrastructure) · **Raised:** 2026-09-02

One missing piece of infrastructure blocks two separate efforts. This is
`docs/agent-builder-plan.md` §4.3, still open after phases 0–2 of the agent
builder landed.

---

## 1. The blocker in one paragraph

`app/mcp/server.py` serves the `query_brain` MCP tool on port **8001**, and port
8001 is a compose-internal service. Nothing routes a public hostname to it.
Anthropic's Managed Agents MCP proxy connects to MCP servers **from Anthropic's
side, over public HTTPS** — so until `https://mcp.brainites.com/mcp` resolves and
terminates TLS, no user-built agent can be grounded in the Brain, and the Claude
plugin marketplace and OpenAI plugin directory both refuse a submission whose
declared MCP host is unreachable.

`mcp.brainites.com` appears in exactly five files today
(`brain-api/app/config/settings.py`, `plugin/.mcp.json`, `.env.example`, and two
docs). **All five are configuration or documentation. Nothing routes it.**

---

## 2. What already works — do not redo this

Phase 0 of the agent-builder plan is complete on `main`. The application side is
ready for an ingress; only the ingress is missing.

| Thing | Where | State |
|---|---|---|
| Streamable HTTP transport (not the deprecated SSE) | `brain-api/app/mcp/server.py:156` — `mcp.run_http_async(transport="http", …)` | ✅ |
| Mount path is `/mcp`, no trailing slash | FastMCP 3.4.2 default `streamable_http_path` | ✅ |
| Accepts `X-API-Key` **and** `Authorization: Bearer` (a vault credential arrives as the latter) | `server.py:_presented_key` | ✅ |
| Fails closed — missing key, invalid key, or missing `brain:query` scope all raise before any data is read | `server.py:_authenticate` | ✅ |
| Plugin declares the same host and transport | `plugin/.mcp.json` — `"type": "http"`, URL byte-identical to `settings.mcp_public_url` | ✅ |
| Container listens on 8001 | `docker-compose.yml` `mcp:` service, `command: python -m app.mcp.server` | ✅ |

**Do not change the URL or the path.** `plugin/.mcp.json` and
`settings.mcp_public_url` must stay byte-identical, including the absence of a
trailing slash — FastMCP mounts at `/mcp`, and a proxy that rewrites to `/mcp/`
introduces a redirect on every call.

---

## 3. The ask

Route `https://mcp.brainites.com/mcp` to the `mcp` container's port 8001.

That is the whole functional requirement. The rest of this section is the set of
constraints that make it work rather than half-work.

### Contract the ingress must satisfy

| Requirement | Why |
|---|---|
| **TLS terminated, HTTPS only.** No plaintext listener, no HTTP→HTTPS-optional. | The request carries a live API key in a header. A plaintext hop puts a workspace credential on the wire. |
| **Preserve `Authorization` and `X-API-Key` headers unmodified.** | Many proxies strip or overwrite `Authorization` by default. Strip it and every Managed Agents session fails auth, because a vault `static_bearer` credential arrives only in that header. |
| **No path rewrite.** `/mcp` in → `/mcp` at the origin. | See above — the URL is pinned in two places and FastMCP mounts exactly `/mcp`. |
| **No response buffering; stream through.** | Streamable HTTP responses are chunked. A proxy that buffers the full body turns streaming into a stall, and with a long extraction (see the 15s budget below) into a timeout. |
| **Read/idle timeout ≥ 60s.** | A `query_brain` miss escalates to live extraction with a 15-second budget, and the whole call can legitimately take tens of seconds. A 30s default timeout will cut real requests. |
| **Set `X-Forwarded-For`** with the true client IP. | Required by the code change in §4. Without it the service is unusable — see the severity note there. |
| **Reject or ignore client-supplied `X-Forwarded-For`.** | Otherwise a caller can spoof their own IP and the rate limit becomes decorative. The proxy must *replace*, not append to, an inbound value it did not set. |
| **Health check target** — see §4.2; there is no `/health` on this service today. | |

### Explicitly *not* required

- No WebSocket support. Streamable HTTP is plain POST + chunked response.
- No CORS configuration. The caller is Anthropic's server-side proxy or the
  plugin binary, never a browser.
- No sticky sessions. Every request authenticates independently from its own
  header; there is no server-side session affinity.
- No separate ingress for the REST API — this is only the `mcp` service.

---

## 4. Two code changes that are ours, not the ingress owner's

These are on the backend team. They must land **with or before** the ingress, not
after. Neither is the infra owner's fault and neither is visible until a proxy is
in front of the service.

### 4.1 ⚠️ Severe: the per-IP rate limit collapses behind any proxy

`_client_ip()` (`app/mcp/server.py:47`) and `get_remote_address()`
(`app/shared/middleware/rate_limit.py:25`) both read `request.client.host` — the
**socket peer** — and nothing in the repo sets `--proxy-headers` or
`forwarded_allow_ips`. Behind a reverse proxy the socket peer is the proxy, so
every request in the world lands in one bucket.

That matters more than it first looks, because
`enforce_api_key_probe_limit(_client_ip())` is called on **every request that
presents a key**, not only on failures (`server.py:96`), and its cap is
`AUTH_LIMIT = 10/minute`.

> **The day the ingress goes live, `query_brain` starts returning "Rate limited"
> after 10 calls per minute across all customers combined.** That is a hard
> outage, not a degradation.

Fix: honour `X-Forwarded-For` on both surfaces — configure the ASGI server's
proxy-header handling with an explicit trusted-proxy allowlist, and make
`_client_ip()` read the forwarded client. Do **not** trust the header
unconditionally; an unauthenticated caller who can set their own key would then
get an unlimited number of key-guessing attempts, which is exactly what this
limit exists to prevent.

Note the per-*workspace* limit (`enforce_brain_query_limit`, `BRAIN_LIMIT =
120/minute`) is keyed on `workspace_id`, not IP, and is unaffected.

The same fix is needed on the REST API if it already sits behind a proxy —
**worth checking whether this is a live issue there today**, since the
`user_key`/`workspace_key` limiters fall back to `get_remote_address` before auth
resolves.

### 4.2 The MCP service has no health-check endpoint

The FastMCP app exposes exactly one route: `/mcp`. There is no `/health`, unlike
the REST app (`app/main.py:98`). A load balancer pointed at `GET /mcp` will not
get a 200 — a streamable-HTTP endpoint rejects a GET without the right `Accept`
negotiation — so a default health check marks the service permanently unhealthy
and the ingress never sends it traffic.

Pick one:
- **Preferred:** add a plain `/health` route to the MCP app so the check is
  unambiguous and matches the REST service's shape.
- Or: configure the health check to accept the specific status `GET /mcp`
  actually returns. Brittle — it changes with a FastMCP upgrade.

---

## 5. Open questions — only the infra owner can answer these

1. **Where is this deployed today?** The repo has no nginx, Caddy, Kubernetes,
   Terraform, `fly.toml`, `render.yaml`, or `.github/` workflows. `docker-compose.yml`
   is the only deployment artifact, and it publishes 8001 to localhost. If a real
   ingress exists outside this repo, that answer changes most of §3.
2. **Does `mcp.brainites.com` already have a DNS record?** If it points anywhere
   today, what?
3. **Does the REST API already sit behind a proxy?** If yes, §4.1 is probably a
   live bug in production right now rather than a future one.
4. **Which transport does the OpenAI plugin directory accept?** Still open from
   agent-builder-plan §4.1, and it needs confirming from OpenAI's own current docs
   before the submitted URL is frozen — `docs/PLUGIN_BUILD.md`'s record on
   unverified platform details is seven errors deep, so do not inherit it from there.

---

## 6. Acceptance test

Run from **outside** the network the service sits in. All four must pass.

```bash
HOST=https://mcp.brainites.com/mcp

# 1. TLS terminates and the host resolves to our service (not a parked page).
curl -sS -o /dev/null -w '%{http_code} %{ssl_verify_result}\n' "$HOST"

# 2. An unauthenticated call is refused — proves auth is reached, not bypassed.
curl -sS -X POST "$HOST" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
# expect: an error mentioning a missing credential

# 3. A real key lists the tool. Both header shapes must work — Managed Agents
#    sends the Bearer form, the plugin sends X-API-Key.
for H in "X-API-Key: $BRAINITE_API_KEY" "Authorization: Bearer $BRAINITE_API_KEY"; do
  curl -sS -X POST "$HOST" \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -H "$H" \
    -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
done
# expect: both list a tool named query_brain

# 4. The rate limit is per-client, not global (§4.1). Issue 15 authenticated
#    calls in under a minute from ONE machine, then one from a DIFFERENT
#    public IP. The second machine's call must succeed.
```

Test 4 is the one that catches §4.1, and it is the one most likely to be skipped.
Do not sign this off without it — everything else can pass while the service is
capped at 10 requests per minute worldwide.

### End-to-end, once the above is green

1. Point a Managed Agents session at the host with a `static_bearer` vault
   credential and confirm the agent can call `query_brain` (agent-builder plan
   phase 4).
2. Install the plugin in a real repo, run `brain enable`, and confirm
   `UserPromptSubmit` injects a matched skill (`plugin/README.md`).

---

## 7. Why this is worth doing before more feature work

From agent-builder-plan §4.3: *"One ingress closes two efforts."*

Until it lands, phases 4–7 of the agent builder can be built but **not tested end
to end** — the runtime half of the product is a mock. And the plugin, which is
feature-complete through milestone 4, cannot be distributed at all. Both efforts
are otherwise unblocked and both are waiting on this single piece of
infrastructure.
