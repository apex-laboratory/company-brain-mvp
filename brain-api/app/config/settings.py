"""Validated application configuration.

All env vars flow through this module (BACKEND_BEST_PRACTICES.md §4). Pydantic
Settings validates every value at import time and raises on failure, so the
process refuses to boot half-configured (fail fast). Import the singleton
``settings`` everywhere; never read ``os.environ`` outside ``config/``.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    environment: Literal["development", "test", "production"] = "development"
    port: int = 4000
    mcp_port: int = 8001  # FastMCP query_brain server (its own process)

    database_url: str  # postgresql+asyncpg://... — PRIVILEGED role (may BYPASSRLS)
    # Restricted, RLS-subject role used for ALL tenant-scoped traffic
    # (tenant_session / run_in_tenant). Provision it with
    # scripts/provision_tenant_role.py. When unset the app falls back to
    # database_url and logs a loud warning — RLS then does NOT isolate tenants,
    # so this MUST be set in any shared/production deployment.
    tenant_database_url: str | None = None
    redis_url: str  # redis://...

    jwt_access_secret: str  # >= 32 chars
    jwt_refresh_secret: str  # >= 32 chars
    encryption_key: str  # exactly 64 hex chars = 32-byte AES key

    # NoDecode: keep the raw env string so the validator below can split it on
    # commas (otherwise pydantic-settings tries to JSON-decode list fields).
    allowed_origins: Annotated[list[str], NoDecode]
    # Host header allow-list for TrustedHostMiddleware. Defaults to "*" for
    # local/test; production must set explicit hostnames (e.g. "api.example.com").
    allowed_hosts: Annotated[list[str], NoDecode] = ["*"]

    ai_service_url: str
    ai_service_token: str
    resend_api_key: str
    default_from_email: str = "Brainite <founders@brainites.com>"

    # Public dashboard origin used to build links in transactional emails
    # (e.g. the member-invite acceptance URL). Frontend base, not the API host.
    app_base_url: str = "http://localhost:3000"

    # ── login SSO (Sign in with Google/GitHub) ────────────────────────────────
    # Separate OAuth client registrations from the source-connector ones below —
    # different scopes (profile/email only) and consent screens.
    login_google_client_id: str = ""
    login_google_client_secret: str = ""
    login_github_client_id: str = ""
    login_github_client_secret: str = ""

    # ── source connectors (KAN-2) ─────────────────────────────────────────────
    # Base URL used to construct the redirect_uri / webhook callback URLs sent to
    # providers. Must match a redirect registered in each provider's app config.
    oauth_redirect_base_url: str = "http://localhost:4000"
    frontend_url: str = "http://localhost:3000"
    # Additional allowlisted frontend origins (comma-separated) the source-connector
    # callback may redirect to — see SourcesService._match_frontend_origin.
    frontend_urls: Annotated[list[str], NoDecode] = []
    # Login SSO (Google/GitHub) redirects the browser to this FRONTEND page, which
    # reads ?code&state and POSTs them to /auth/oauth/{provider}/callback. Must be
    # registered verbatim as the "Authorized redirect URI" in each provider's
    # console. Distinct from oauth_redirect_base_url (that's for source-connector
    # callbacks, which the backend receives directly).
    frontend_oauth_callback_path: str = "/auth/callback"
    notion_client_id: str = ""
    notion_client_secret: str = ""
    # GitHub App (KAN-7). Auth is App-JWT (RS256) → per-installation tokens, so no
    # OAuth client id/secret. The private key is a PEM stored \n-escaped on one line.
    github_app_id: str = ""
    github_app_private_key: str = ""
    github_app_slug: str = ""  # used to build the install URL
    github_webhook_secret: str = ""  # shared secret for X-Hub-Signature-256
    # GitHub App OAuth client (the App's own client id/secret, distinct from the numeric
    # app id). Used for the "Request user authorization (OAuth) during installation" leg:
    # the callback code is exchanged for a user token so we can verify the caller
    # actually controls the installation_id they passed (blocks cross-tenant binding).
    github_app_client_id: str = ""
    github_app_client_secret: str = ""
    # Google (Drive + Gmail) share one OAuth client (web-server flow). The consent
    # screen is kept in "Testing" status (<=100 users) to avoid Google verification.
    google_client_id: str = ""
    google_client_secret: str = ""
    # Gmail push: the Cloud Pub/Sub topic users.watch publishes to, and a shared token
    # embedded in the Pub/Sub push endpoint URL (?token=) used to verify deliveries.
    google_pubsub_topic: str = ""  # projects/<project>/topics/<topic>
    google_pubsub_verification_token: str = ""
    # Slack connector
    slack_client_id: str = ""
    slack_client_secret: str = ""
    slack_signing_secret: str = ""
    # Zendesk OAuth (subdomain-scoped). One global OAuth client across all customer
    # subdomains; the per-customer subdomain travels through the OAuth flow + connection.
    zendesk_client_id: str = ""
    zendesk_client_secret: str = ""
    # Jira Cloud OAuth 2.0 (3LO). One global OAuth client; consent is at
    # auth.atlassian.com and the customer's Jira site (cloudId) is discovered after
    # exchange via accessible-resources — no per-tenant host to configure.
    jira_client_id: str = ""
    jira_client_secret: str = ""

    # Public MCP endpoint every workspace is handed. Single host, not per-workspace:
    # query_brain resolves the tenant from the caller's X-API-Key, not the Host
    # header, so a wildcard-subdomain-per-workspace scheme would need a DNS/TLS/
    # ingress topology that doesn't exist and buys no isolation. Must match the
    # URL baked into plugin/.mcp.json.
    mcp_public_url: str = "https://mcp.brainites.com/mcp"

    # ── extraction pipeline LLMs (Phase 3) ───────────────────────────────────
    # Empty-string defaults so the API/worker boot without keys; the LLM client
    # raises at first use if a stage needs a missing key (pipeline-only failure).
    gemini_api_key: str = ""
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    openrouter_api_key: str = ""
    # "-latest" alias always resolves to Google's current cheapest flash-lite
    # model, so a future model retirement (like gemini-2.5-flash-lite's, which
    # this pinned) doesn't silently 404 the whole pipeline again. Fine for the
    # fast classifier stages and (for now, while ANTHROPIC_API_KEY is out) the
    # extraction stages too.
    gemini_model: str = "gemini-flash-lite-latest"
    anthropic_model: str = "claude-sonnet-5"
    # OpenRouter is OpenAI-API-compatible; ":free"-suffixed models cost nothing
    # (see pipeline/llm/pricing.py) and need no billing setup — the default
    # picks a testing/demo model without requiring OPENROUTER_MODEL to be set.
    # OpenRouter's free-tier catalog rotates as vendors add/withdraw free
    # capacity — a pinned slug can 404 ("model unavailable for free") without
    # warning. If that happens, set OPENROUTER_MODEL to a currently-free slug
    # from https://openrouter.ai/models?max_price=0. This one is a reasoning
    # model — it narrates chain-of-thought in ``content`` before the answer,
    # which is why ``clients._parse_json`` tolerates leading prose and the
    # tightest-budget stages (relevance_gate, boundary_classifier) use a
    # larger max_tokens than the answer alone needs.
    openrouter_model: str = "nvidia/nemotron-3.5-lightning:free"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    embedding_model: str = "text-embedding-3-small"
    llm_max_attempts: int = 5  # per-call attempts inside the pipeline retry wrapper
    # Single knob that swaps every pipeline stage + brain chat call between
    # providers — no code changes needed, see app/pipeline/llm/providers.py.
    llm_provider: Literal["gemini", "anthropic", "openrouter"] = "gemini"

    # ── brain chat (delivery — BACKEND_ASKS §7) ───────────────────────────────
    # Global kill-switch for the "Ask the brain" chat surface. Ops can hard-disable
    # everywhere; per-workspace readiness (skills indexed) is computed on top of it
    # by the brain readiness gate.
    brain_chat_enabled: bool = True

    # ── agent-run ingestion (Phase 7 — PRD Features 29-34) ────────────────────
    # Caps on a single pushed trace. The step cap is validated in the request
    # schema (422 naming the field) and the byte cap by the ASGI body guard (413):
    # a harness that trips either gets an actionable error, not a truncated run.
    run_max_steps: int = 400
    run_max_body_bytes: int = 1_048_576  # 1 MB
    # A run this short is a conversation turn, not a procedure. Below the floor the
    # gate rejects with ``too_trivial`` rather than spending a distillation on it.
    run_min_steps: int = 3
    # Step args/results larger than this are stored as a digest instead of content
    # (redaction.py). Keeps the trace bounded and matches what the retention job
    # leaves behind, so a step's shape does not change when the body is nulled.
    run_step_payload_max_bytes: int = 2048
    # Cosine floor for two runs to be "the same task". Deliberately stricter than
    # the skill-match threshold (0.70): a false merge here distils two different
    # procedures into one wrong one, where a false match there only returns a less
    # relevant skill. Re-tune after any embedding-model rotation.
    run_cluster_threshold: float = 0.85
    # Runs a cluster needs before it is worth one Sonnet distillation. One success
    # is an anecdote (luck, a warm cache, a path that works for one customer);
    # three converging on the same spine is evidence. This is the cost control:
    # an agent running a task 500 times yields one distillation, not 500.
    # ``humanConfirmed`` runs bypass it entirely.
    run_min_runs_per_cluster: int = 3
    # Days a raw trace body survives before the retention job NULLs it. The digest
    # and any distilled skill outlive it.
    run_retention_days: int = 30

    # source_authority.yaml (sweep processing order etc.); lives at the repo root
    # in dev. A missing file falls back to the built-in default order.
    source_authority_path: str = "../source_authority.yaml"

    # ── token lifetimes (seconds) ────────────────────────────────────────────
    access_token_ttl_seconds: int = 15 * 60  # 15 minutes
    refresh_token_ttl_seconds: int = 30 * 24 * 60 * 60  # 30 days
    oauth_state_ttl_seconds: int = 10 * 60  # 10 minutes

    @field_validator("jwt_access_secret", "jwt_refresh_secret", mode="after")
    @classmethod
    def min_length_32(cls, v: str) -> str:
        if len(v) < 32:
            raise ValueError("must be at least 32 characters")
        return v

    @field_validator("encryption_key", mode="after")
    @classmethod
    def must_be_64_hex(cls, v: str) -> str:
        if len(v) != 64:
            raise ValueError("must be 64 hex characters (32 bytes)")
        try:
            bytes.fromhex(v)
        except ValueError as exc:
            raise ValueError("must be valid hex") from exc
        return v

    @field_validator("allowed_origins", "allowed_hosts", "frontend_urls", mode="before")
    @classmethod
    def parse_csv_list(cls, v: str | list[str]) -> list[str]:
        if isinstance(v, str):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()  # values supplied from the environment


settings = get_settings()
