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

    # Base domain for per-workspace MCP/brain endpoints. The workspace settings
    # endpoint advertises ``https://{slug}.{mcp_base_domain}/mcp`` to clients.
    mcp_base_domain: str = "brainites.com"

    # ── OAuth providers (login SSO) ───────────────────────────────────────────
    google_client_id: str = ""
    google_client_secret: str = ""
    github_client_id: str = ""
    github_client_secret: str = ""
    # Base URL used to construct the redirect_uri sent to providers.
    # Must match a redirect registered in each provider's app config.
    oauth_redirect_base_url: str = "http://localhost:4000"

    # ── Source-connection OAuth providers ─────────────────────────────────────
    # Read-only knowledge-source connectors. Each empty by default so the app
    # boots unconfigured; connecting an unconfigured provider returns 501.
    # GitHub reuses github_client_id/github_client_secret above; Google Drive
    # reuses google_client_id/google_client_secret above.
    slack_client_id: str = ""
    slack_client_secret: str = ""
    notion_client_id: str = ""
    notion_client_secret: str = ""
    jira_client_id: str = ""
    jira_client_secret: str = ""
    zendesk_client_id: str = ""
    zendesk_client_secret: str = ""
    # Zendesk OAuth is per-account: its URLs are {subdomain}.zendesk.com.
    zendesk_subdomain: str = ""

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
