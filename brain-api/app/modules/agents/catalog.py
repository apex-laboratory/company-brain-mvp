"""The connector catalog — ``agent_connectors.yaml`` read into typed rows.

**Connectors are data, not code** (agent-builder-plan §2). Everything a provider
needs is a row in the YAML plus an OAuth client registration; nothing here knows
the name of any particular provider. The one deliberate difference from
``app/pipeline/authority.py``, which this otherwise mirrors, is failure posture:
authority is fail-soft because a bad config there costs precision, whereas a bad
config *here* would offer a user a Connect button that bounces them to a consent
screen we cannot complete. So a malformed row is dropped and logged, and a
malformed file yields an empty catalog.

Three rules the shape encodes:

* **``mcp_server_url`` is an identity, not a link.** We never fetch it — Anthropic
  does, at run time. It is also the key a vault stores the credential under, so it
  must be byte-identical to the agent's ``mcp_servers`` entry, trailing slash and
  all. A row without one is not a provider we can support yet, so it is skipped
  rather than listed as broken.
* **Client secrets are read from the environment, never from the YAML.** The row
  names the env vars; the file itself stays safe to commit. That also keeps
  "add a provider" a config change rather than a ``settings.py`` edit, which is
  the whole point of the data-not-code rule.
* **``configured`` is a first-class field.** A provider whose env vars are unset
  is still catalogued, marked unconfigured, so the picker can grey it out with a
  reason instead of letting the user start a flow that ends in a 501.

The module memoizes on first read. A catalog change is a deploy, like the
authority config — there is no reload endpoint, and adding one would mean
reasoning about a half-swapped catalog mid-OAuth-dance.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from app.config.settings import settings

log = logging.getLogger(__name__)

# How the token endpoint wants the client credentials presented. Mirrors
# Anthropic's vault ``token_endpoint_auth`` union exactly, because the value is
# forwarded verbatim into the credential's refresh block — a third spelling here
# would be one translation table nobody remembers to update.
TokenEndpointAuth = Literal["client_secret_post", "client_secret_basic", "none"]

_TOKEN_AUTH_VALUES = frozenset({"client_secret_post", "client_secret_basic", "none"})

# A provider key becomes a URL path segment (`/agent-credentials/{provider}/…`)
# and an oauth_states discriminator, so it is restricted rather than free text.
_PROVIDER_KEY = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_")


@dataclass(frozen=True)
class ConnectorSpec:
    """One catalogued provider: where its MCP server is, and how to authorize it."""

    provider: str
    display_name: str
    mcp_server_url: str
    authorize_endpoint: str
    token_endpoint: str
    token_endpoint_auth: TokenEndpointAuth
    description: str | None = None
    docs_url: str | None = None
    scopes: str | None = None
    accept_json: bool = False
    extra_authorize_params: dict[str, str] = field(default_factory=dict)
    # Env var *names*, not values. Resolved on demand so a secret never sits in a
    # long-lived dataclass that might end up in a log line or an error body.
    client_id_env: str = ""
    client_secret_env: str = ""

    @property
    def client_id(self) -> str:
        return os.environ.get(self.client_id_env, "") if self.client_id_env else ""

    @property
    def client_secret(self) -> str:
        return os.environ.get(self.client_secret_env, "") if self.client_secret_env else ""

    @property
    def configured(self) -> bool:
        """Whether this deployment can actually run the flow.

        ``none`` client auth is a public OAuth client — it has an id and
        legitimately has no secret, so requiring one would make every public
        client permanently unconfigured.
        """
        if not self.client_id:
            return False
        return self.token_endpoint_auth == "none" or bool(self.client_secret)


def _row(provider: str, raw: Any) -> ConnectorSpec | None:
    """Validate one YAML row. Returns ``None`` for a row that must not be offered."""
    if not isinstance(raw, dict):
        log.warning("agent catalog: %r is not a mapping; skipped", provider)
        return None
    if not provider or set(provider) - _PROVIDER_KEY:
        log.warning("agent catalog: %r is not a valid provider key; skipped", provider)
        return None

    mcp_server_url = str(raw.get("mcp_server_url") or "").strip()
    if not mcp_server_url:
        # A row kept for its OAuth block against the day a hosted server exists.
        # Not an error, and deliberately not surfaced as "coming soon" either —
        # the picker should only ever show what a user can finish connecting.
        log.info("agent catalog: %r has no mcp_server_url yet; not offered", provider)
        return None
    if not mcp_server_url.startswith("https://"):
        # Anthropic carries a vault credential to this URL; a plaintext hop would
        # put that credential on the wire.
        log.warning("agent catalog: %r mcp_server_url is not https; skipped", provider)
        return None

    authorize_endpoint = str(raw.get("authorize_endpoint") or "").strip()
    token_endpoint = str(raw.get("token_endpoint") or "").strip()
    if not authorize_endpoint.startswith("https://") or not token_endpoint.startswith("https://"):
        log.warning(
            "agent catalog: %r needs https authorize_endpoint and token_endpoint; skipped",
            provider,
        )
        return None

    token_endpoint_auth = str(raw.get("token_endpoint_auth") or "client_secret_post")
    if token_endpoint_auth not in _TOKEN_AUTH_VALUES:
        log.warning(
            "agent catalog: %r has unknown token_endpoint_auth %r; skipped",
            provider, token_endpoint_auth,
        )
        return None

    extra = raw.get("extra_authorize_params") or {}
    if not isinstance(extra, dict):
        log.warning("agent catalog: %r extra_authorize_params is not a mapping; skipped", provider)
        return None

    return ConnectorSpec(
        provider=provider,
        display_name=str(raw.get("display_name") or provider),
        mcp_server_url=mcp_server_url,
        authorize_endpoint=authorize_endpoint,
        token_endpoint=token_endpoint,
        token_endpoint_auth=token_endpoint_auth,  # type: ignore[arg-type]
        description=raw.get("description"),
        docs_url=raw.get("docs_url"),
        scopes=(str(raw["scopes"]).strip() or None) if raw.get("scopes") else None,
        accept_json=bool(raw.get("accept_json")),
        extra_authorize_params={str(k): str(v) for k, v in extra.items()},
        client_id_env=str(raw.get("client_id_env") or ""),
        client_secret_env=str(raw.get("client_secret_env") or ""),
    )


def _load(path: str | Path | None = None) -> dict[str, ConnectorSpec]:
    resolved = Path(path or settings.agent_connectors_path)
    try:
        config = yaml.safe_load(resolved.read_text())
        if not isinstance(config, dict):
            raise ValueError("agent_connectors.yaml is not a mapping")
        connectors = config.get("connectors") or {}
        if not isinstance(connectors, dict):
            raise ValueError("agent_connectors.yaml has no `connectors` mapping")
    except Exception:
        log.warning(
            "agent catalog: could not read %s; the connector catalog is empty",
            resolved, exc_info=True,
        )
        return {}

    catalog: dict[str, ConnectorSpec] = {}
    for provider, raw in connectors.items():
        spec = _row(str(provider), raw)
        if spec is not None:
            catalog[spec.provider] = spec
    return catalog


_catalog: dict[str, ConnectorSpec] | None = None


def load_catalog(path: str | Path | None = None) -> dict[str, ConnectorSpec]:
    """The catalog, memoized. Pass ``path`` in tests to read a fixture instead."""
    global _catalog
    if path is not None:
        return _load(path)
    if _catalog is None:
        _catalog = _load()
    return _catalog


def list_connectors() -> list[ConnectorSpec]:
    """Every catalogued provider, ordered by display name for a stable picker."""
    return sorted(load_catalog().values(), key=lambda spec: spec.display_name.lower())


def get_connector(provider: str) -> ConnectorSpec | None:
    """One provider's spec, or ``None`` if it is not catalogued on this deployment."""
    return load_catalog().get(provider)


def reset_cache() -> None:
    """Drop the memoized catalog. For tests only — production reloads by deploy."""
    global _catalog
    _catalog = None
