"""Connector-catalog loading (agent-builder-plan §2, phase 2).

The catalog is the file a deployment edits to add a provider, so what these
tests pin is its **failure posture**, not its happy path. A malformed row must
not reach the picker: every entry the catalog returns is a Connect button, and a
button that bounces the user to a broken consent screen is worse than an absent
one. So each rejection below corresponds to a specific way a hand-edited YAML
goes wrong.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.modules.agents import catalog

_GOOD = """
version: 1
connectors:
  linear:
    display_name: Linear
    description: Issues and projects.
    mcp_server_url: https://mcp.linear.app/mcp
    authorize_endpoint: https://linear.app/oauth/authorize
    token_endpoint: https://api.linear.app/oauth/token
    token_endpoint_auth: client_secret_post
    scopes: read write
    client_id_env: TEST_LINEAR_CLIENT_ID
    client_secret_env: TEST_LINEAR_CLIENT_SECRET
"""


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "agent_connectors.yaml"
    path.write_text(body)
    return path


def test_a_valid_row_loads_with_its_fields(tmp_path: Path) -> None:
    spec = catalog.load_catalog(_write(tmp_path, _GOOD))["linear"]

    assert spec.display_name == "Linear"
    assert spec.mcp_server_url == "https://mcp.linear.app/mcp"
    assert spec.token_endpoint_auth == "client_secret_post"
    assert spec.scopes == "read write"


def test_a_row_with_no_mcp_server_url_is_not_offered(tmp_path: Path) -> None:
    """The Slack case: an OAuth block kept ready for a server that does not exist.

    Skipped rather than listed-as-unavailable — the picker should only ever show
    what a user can finish connecting.
    """
    body = _GOOD.replace("https://mcp.linear.app/mcp", '""')
    assert catalog.load_catalog(_write(tmp_path, body)) == {}


@pytest.mark.parametrize(
    "field",
    ["mcp_server_url", "authorize_endpoint", "token_endpoint"],
)
def test_a_plaintext_url_is_rejected(tmp_path: Path, field: str) -> None:
    """Every one of these carries either a client secret or a vault credential."""
    lines = []
    for line in _GOOD.splitlines():
        if line.strip().startswith(f"{field}:"):
            line = line.replace("https://", "http://")
        lines.append(line)
    assert catalog.load_catalog(_write(tmp_path, "\n".join(lines))) == {}


def test_an_unknown_token_endpoint_auth_is_rejected(tmp_path: Path) -> None:
    """The value is forwarded verbatim into Anthropic's refresh block.

    A typo here would not fail until a vault credential silently stopped
    refreshing, days later.
    """
    body = _GOOD.replace("client_secret_post", "client_secret_jwt")
    assert catalog.load_catalog(_write(tmp_path, body)) == {}


def test_a_provider_key_that_is_not_url_safe_is_rejected(tmp_path: Path) -> None:
    """The key becomes a path segment and an oauth_states discriminator."""
    body = _GOOD.replace("  linear:", "  Linear/App:")
    assert catalog.load_catalog(_write(tmp_path, body)) == {}


def test_a_bad_row_does_not_take_the_good_ones_with_it(tmp_path: Path) -> None:
    body = _GOOD + """
  broken:
    display_name: Broken
    mcp_server_url: https://example.test/mcp
    authorize_endpoint: not-a-url
    token_endpoint: https://example.test/token
"""
    loaded = catalog.load_catalog(_write(tmp_path, body))
    assert set(loaded) == {"linear"}


def test_a_missing_file_yields_an_empty_catalog(tmp_path: Path) -> None:
    """Deliberately *not* fail-soft-with-defaults like source_authority.yaml.

    There is no safe default connector list: guessing one would offer providers
    this deployment has no OAuth client for.
    """
    assert catalog.load_catalog(tmp_path / "absent.yaml") == {}


def test_a_file_that_is_not_a_mapping_yields_an_empty_catalog(tmp_path: Path) -> None:
    assert catalog.load_catalog(_write(tmp_path, "- just\n- a\n- list\n")) == {}


# ── configured ────────────────────────────────────────────────────────────────


def test_configured_is_false_without_client_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TEST_LINEAR_CLIENT_ID", raising=False)
    monkeypatch.delenv("TEST_LINEAR_CLIENT_SECRET", raising=False)
    assert catalog.load_catalog(_write(tmp_path, _GOOD))["linear"].configured is False


def test_configured_is_true_once_both_env_vars_are_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_LINEAR_CLIENT_ID", "id")
    monkeypatch.setenv("TEST_LINEAR_CLIENT_SECRET", "secret")
    spec = catalog.load_catalog(_write(tmp_path, _GOOD))["linear"]
    assert spec.configured is True
    assert spec.client_id == "id"


def test_a_public_client_needs_no_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``token_endpoint_auth: none`` is a public client — it legitimately has no
    secret, and requiring one would leave it permanently unconfigured."""
    monkeypatch.setenv("TEST_LINEAR_CLIENT_ID", "id")
    monkeypatch.delenv("TEST_LINEAR_CLIENT_SECRET", raising=False)
    body = _GOOD.replace("client_secret_post", "none")
    assert catalog.load_catalog(_write(tmp_path, body))["linear"].configured is True


def test_the_secret_is_read_from_the_environment_not_held_on_the_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dataclass stores env var *names*. A spec that captured the value could
    end up in a repr, a log line or an error body."""
    spec = catalog.load_catalog(_write(tmp_path, _GOOD))["linear"]
    monkeypatch.setenv("TEST_LINEAR_CLIENT_SECRET", "set-after-load")

    assert "set-after-load" not in repr(spec)
    assert spec.client_secret == "set-after-load"


# ── the catalog this repo actually ships ──────────────────────────────────────


def test_the_shipped_catalog_parses() -> None:
    """A YAML mistake in the committed file would make the picker silently empty."""
    catalog.reset_cache()
    try:
        providers = {spec.provider for spec in catalog.list_connectors()}
    finally:
        catalog.reset_cache()

    assert providers, "agent_connectors.yaml produced no connectors"
    # Slack ships with a blank mcp_server_url until a hosted server exists.
    assert "slack" not in providers
