"""Read-only source-connection OAuth (BACKEND_BEST_PRACTICES.md §1, §12).

One generic OAuth2 authorization-code connector driven by a per-provider config
table, rather than five near-identical modules — adding/adjusting a provider is a
single ``_PROVIDERS`` entry. Covers Slack, Notion, GitHub, Jira (Atlassian), and
Zendesk. All HTTP uses a 10-second timeout.

The token exchange returns a normalized :class:`SourceToken` (access/refresh
token, expiry, and the provider account id + display name) so the service layer
never sees provider-specific response shapes. Credentials come from validated
settings; a provider with no configured client raises :class:`SourceOAuthError`
(501) so an unconfigured connector fails loud, not silently.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlencode

import httpx

from app.config.settings import settings

SourceProvider = Literal["slack", "notion", "github", "jira", "zendesk"]
_TIMEOUT = httpx.Timeout(10.0)

# Token-endpoint authentication styles across providers:
#   "form"       — client_id/secret in the form body (Slack, GitHub, Zendesk)
#   "json_basic" — JSON body + HTTP Basic client credentials (Notion, Jira)
AuthStyle = Literal["form", "json_basic"]


class SourceOAuthError(Exception):
    """Provider OAuth failed or the provider is not configured.

    Carries the HTTP status/code the API layer should surface so a configuration
    gap (501) or a provider rejection (502) never leaks as a generic 500.
    """

    def __init__(
        self, message: str, *, status: int = 502, code: str = "provider_error"
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True)
class SourceToken:
    """Normalized result of a source OAuth code exchange."""

    access_token: str
    refresh_token: str | None
    expires_in: int | None
    external_account_id: str | None
    account_name: str
    scopes: list[str]


@dataclass(frozen=True)
class _ProviderConfig:
    display_name: str
    tag: str  # short UI descriptor (API_DOCUMENTATION.md §List Source Providers)
    authorize_url: Callable[[], str]
    token_url: Callable[[], str]
    default_scopes: tuple[str, ...]
    auth_style: AuthStyle
    cred_attrs: tuple[str, str]  # (client_id setting, client_secret setting)
    # Pull (external_account_id, account_name) out of the token response.
    parse_account: Callable[[Mapping[str, Any]], tuple[str | None, str]]
    extra_authorize_params: Mapping[str, str]


def _zendesk_base() -> str:
    if not settings.zendesk_subdomain:
        raise SourceOAuthError(
            "Zendesk is not configured (ZENDESK_SUBDOMAIN missing).",
            status=501,
            code="provider_not_configured",
        )
    return f"https://{settings.zendesk_subdomain}.zendesk.com"


_PROVIDERS: dict[str, _ProviderConfig] = {
    "slack": _ProviderConfig(
        display_name="Slack",
        tag="Conversations & decisions",
        authorize_url=lambda: "https://slack.com/oauth/v2/authorize",
        token_url=lambda: "https://slack.com/api/oauth.v2.access",
        default_scopes=("channels:history", "channels:read"),
        auth_style="form",
        cred_attrs=("slack_client_id", "slack_client_secret"),
        parse_account=lambda d: (
            (d.get("team") or {}).get("id"),
            (d.get("team") or {}).get("name") or "Slack",
        ),
        extra_authorize_params={},
    ),
    "notion": _ProviderConfig(
        display_name="Notion",
        tag="Policies & playbooks",
        authorize_url=lambda: "https://api.notion.com/v1/oauth/authorize",
        token_url=lambda: "https://api.notion.com/v1/oauth/token",
        default_scopes=(),  # Notion uses integration capabilities, not scopes
        auth_style="json_basic",
        cred_attrs=("notion_client_id", "notion_client_secret"),
        parse_account=lambda d: (
            d.get("workspace_id"),
            d.get("workspace_name") or "Notion",
        ),
        extra_authorize_params={"owner": "user"},
    ),
    "github": _ProviderConfig(
        display_name="GitHub",
        tag="Code & review decisions",
        authorize_url=lambda: "https://github.com/login/oauth/authorize",
        token_url=lambda: "https://github.com/login/oauth/access_token",
        default_scopes=("repo", "read:org"),
        auth_style="form",
        cred_attrs=("github_client_id", "github_client_secret"),
        parse_account=lambda d: (None, "GitHub"),
        extra_authorize_params={},
    ),
    "jira": _ProviderConfig(
        display_name="Jira",
        tag="Tickets & incidents",
        authorize_url=lambda: "https://auth.atlassian.com/authorize",
        token_url=lambda: "https://auth.atlassian.com/oauth/token",
        default_scopes=("read:jira-work", "offline_access"),
        auth_style="json_basic",
        cred_attrs=("jira_client_id", "jira_client_secret"),
        parse_account=lambda d: (None, "Jira"),
        extra_authorize_params={"audience": "api.atlassian.com", "prompt": "consent"},
    ),
    "zendesk": _ProviderConfig(
        display_name="Zendesk",
        tag="Support resolutions",
        authorize_url=lambda: f"{_zendesk_base()}/oauth/authorizations/new",
        token_url=lambda: f"{_zendesk_base()}/oauth/tokens",
        default_scopes=("read",),
        auth_style="form",
        cred_attrs=("zendesk_client_id", "zendesk_client_secret"),
        parse_account=lambda _d: (settings.zendesk_subdomain or None, "Zendesk"),
        extra_authorize_params={},
    ),
}

PROVIDERS: frozenset[str] = frozenset(_PROVIDERS)


def provider_catalog() -> list[dict[str, Any]]:
    """Static provider catalog for ``GET /sources/providers``."""
    return [
        {
            "provider": key,
            "name": cfg.display_name,
            "tag": cfg.tag,
            "defaultScopes": list(cfg.default_scopes),
            "readOnly": True,
        }
        for key, cfg in _PROVIDERS.items()
    ]


def default_scopes(provider: str) -> list[str]:
    return list(_PROVIDERS[provider].default_scopes)


def _credentials(cfg: _ProviderConfig) -> tuple[str, str]:
    client_id = getattr(settings, cfg.cred_attrs[0])
    client_secret = getattr(settings, cfg.cred_attrs[1])
    if not client_id or not client_secret:
        raise SourceOAuthError(
            f"{cfg.display_name} is not configured.",
            status=501,
            code="provider_not_configured",
        )
    return client_id, client_secret


def build_authorize_url(
    *, provider: str, redirect_uri: str, state: str, scopes: list[str]
) -> str:
    """Build the provider's authorization URL with the requested scopes."""
    cfg = _PROVIDERS[provider]
    client_id, _ = _credentials(cfg)
    params: dict[str, str] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "state": state,
        **cfg.extra_authorize_params,
    }
    return f"{cfg.authorize_url()}?{urlencode(params)}"


async def exchange_code(
    *, provider: str, code: str, redirect_uri: str, scopes: list[str]
) -> SourceToken:
    """Exchange an authorization code for a normalized :class:`SourceToken`."""
    cfg = _PROVIDERS[provider]
    client_id, client_secret = _credentials(cfg)

    body = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    }
    kwargs: dict[str, Any] = {"headers": {"Accept": "application/json"}}
    if cfg.auth_style == "json_basic":
        kwargs["json"] = body
        kwargs["auth"] = httpx.BasicAuth(client_id, client_secret)
    else:  # "form"
        kwargs["data"] = {**body, "client_id": client_id, "client_secret": client_secret}

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(cfg.token_url(), **kwargs)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as exc:
        raise SourceOAuthError(f"{cfg.display_name} token exchange failed.") from exc

    if not isinstance(data, dict):
        raise SourceOAuthError(f"{cfg.display_name} returned an unexpected response.")
    # Slack signals app-level failures with ok=false and HTTP 200.
    if data.get("ok") is False:
        raise SourceOAuthError(
            f"{cfg.display_name} rejected the authorization: {data.get('error')}"
        )

    access_token = data.get("access_token")
    if not access_token:
        raise SourceOAuthError(f"{cfg.display_name} returned no access token.")

    external_account_id, account_name = cfg.parse_account(data)
    granted = data.get("scope")
    granted_scopes = granted.replace(",", " ").split() if isinstance(granted, str) else scopes
    expires_in = data.get("expires_in")
    return SourceToken(
        access_token=str(access_token),
        refresh_token=data.get("refresh_token"),
        expires_in=int(expires_in) if isinstance(expires_in, int) else None,
        external_account_id=external_account_id,
        account_name=account_name,
        scopes=granted_scopes,
    )
