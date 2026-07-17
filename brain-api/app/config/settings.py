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

    database_url: str  # postgresql+asyncpg://...
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

    # Base domain for per-workspace MCP/brain endpoints.
    mcp_base_domain: str = "brainites.com"

    # ── extraction pipeline LLMs (Phase 3) ───────────────────────────────────
    # Empty-string defaults so the API/worker boot without keys; the LLM client
    # raises at first use if a stage needs a missing key (pipeline-only failure).
    groq_api_key: str = ""
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"
    anthropic_model: str = "claude-sonnet-5"
    embedding_model: str = "text-embedding-3-small"
    llm_max_attempts: int = 3  # per-call attempts inside the pipeline retry wrapper

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

    @field_validator("allowed_origins", "allowed_hosts", mode="before")
    @classmethod
    def parse_csv_list(cls, v: str | list[str]) -> list[str]:
        if isinstance(v, str):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()  # values supplied from the environment


settings = get_settings()
