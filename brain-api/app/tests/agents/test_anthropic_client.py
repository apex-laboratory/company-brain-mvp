"""The Anthropic seam: error translation and payload shape (agent-builder-plan §5.2).

Two things are worth testing here and nothing else is:

* **Error translation**, because it is the whole reason the seam exists. A
  vendor error reaching a router renders as a 500 with a vendor-shaped body, and
  the distinction the dashboard needs — "your version is stale, reload" versus
  "Anthropic is down, retry" — lives only in this mapping.
* **Payload construction**, specifically that ``None`` means *omit*. On this API
  an explicit null **clears** a field, so a create that helpfully passed
  ``system=None`` through would wipe a system prompt the caller never mentioned.

The SDK itself is stubbed. Testing that ``agents.create`` reaches Anthropic is
Anthropic's job; testing that we call it with the right shape is ours.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import anthropic
import httpx
import pytest

from app.modules.agents.anthropic_client import AgentRuntimeError, AnthropicAgentsClient
from app.shared.errors.app_error import (
    ConfigurationError,
    ConflictError,
    NotFoundError,
    ValidationError,
)


def _agent(agent_id: str = "agent_1", version: int = 1) -> MagicMock:
    stub = MagicMock()
    stub.id = agent_id
    stub.version = version
    stub.name = "Refunds"
    return stub


def _client_with(create: Any = None, update: Any = None) -> AnthropicAgentsClient:
    """A seam whose lazily-built SDK is already replaced by a stub."""
    client = AnthropicAgentsClient()
    sdk = MagicMock()
    sdk.beta.agents.create = create or AsyncMock(return_value=_agent())
    sdk.beta.agents.update = update or AsyncMock(return_value=_agent(version=2))
    client._client = sdk
    return client


def _api_error(cls: type, status: int) -> Exception:
    """Build a real SDK exception — its constructor needs a response object."""
    request = httpx.Request("POST", "https://api.anthropic.com/v1/agents")
    response = httpx.Response(status, request=request)
    return cls("boom", response=response, body=None)


# ── payload construction ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_omits_unset_fields_rather_than_sending_null() -> None:
    """``None`` must not reach the API: an explicit null clears the field."""
    create = AsyncMock(return_value=_agent())
    client = _client_with(create=create)

    await client.create_agent(name="Refunds", model="claude-opus-5")

    sent = create.await_args.kwargs
    assert sent == {"name": "Refunds", "model": "claude-opus-5"}
    assert "system" not in sent
    assert "tools" not in sent


@pytest.mark.asyncio
async def test_create_returns_id_and_version_not_the_sdk_object() -> None:
    """Callers get plain dicts — an SDK type here would leak into the API contract."""
    client = _client_with(create=AsyncMock(return_value=_agent("agent_9", 3)))

    result = await client.create_agent(name="Refunds", model="claude-opus-5")

    assert result == {"id": "agent_9", "version": 3}


@pytest.mark.asyncio
async def test_update_passes_version_for_optimistic_concurrency() -> None:
    update = AsyncMock(return_value=_agent(version=4))
    client = _client_with(update=update)

    await client.update_agent("agent_1", version=3, system="be helpful")

    assert update.await_args.args == ("agent_1",)
    assert update.await_args.kwargs == {"version": 3, "system": "be helpful"}


# ── error translation ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_conflict_becomes_domain_conflict_with_a_reload_hint() -> None:
    """A 409 is the caller's problem and is fixable — say how."""
    create = AsyncMock(side_effect=_api_error(anthropic.ConflictError, 409))
    client = _client_with(create=create)

    with pytest.raises(ConflictError) as exc:
        await client.create_agent(name="Refunds", model="claude-opus-5")

    assert "Reload" in exc.value.message
    assert exc.value.status == 409


@pytest.mark.asyncio
async def test_not_found_becomes_domain_not_found() -> None:
    create = AsyncMock(side_effect=_api_error(anthropic.NotFoundError, 404))
    client = _client_with(create=create)

    with pytest.raises(NotFoundError) as exc:
        await client.create_agent(name="Refunds", model="claude-opus-5")

    assert exc.value.status == 404
    assert exc.value.message == "Agent not found."


@pytest.mark.asyncio
async def test_bad_request_carries_the_vendor_message_in_details() -> None:
    """The vendor names the offending field; we must not swallow that."""
    create = AsyncMock(side_effect=_api_error(anthropic.BadRequestError, 400))
    client = _client_with(create=create)

    with pytest.raises(ValidationError) as exc:
        await client.create_agent(name="Refunds", model="nope")

    assert exc.value.status == 422
    assert "agentRuntime" in exc.value.details


@pytest.mark.asyncio
async def test_auth_failure_becomes_configuration_error_not_a_client_error() -> None:
    """A rejected key is an operator problem — a 401 to the dashboard user would
    read as *their* session expiring, which is the wrong thing to act on."""
    create = AsyncMock(side_effect=_api_error(anthropic.AuthenticationError, 401))
    client = _client_with(create=create)

    with pytest.raises(ConfigurationError) as exc:
        await client.create_agent(name="Refunds", model="claude-opus-5")

    assert exc.value.status == 501


@pytest.mark.asyncio
async def test_upstream_5xx_becomes_502_not_500() -> None:
    """Ours vs theirs: 502 tells the operator where to look and the client to retry."""
    create = AsyncMock(side_effect=_api_error(anthropic.InternalServerError, 500))
    client = _client_with(create=create)

    with pytest.raises(AgentRuntimeError) as exc:
        await client.create_agent(name="Refunds", model="claude-opus-5")

    assert exc.value.status == 502


@pytest.mark.asyncio
async def test_connection_error_becomes_502() -> None:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/agents")
    create = AsyncMock(side_effect=anthropic.APIConnectionError(request=request))
    client = _client_with(create=create)

    with pytest.raises(AgentRuntimeError):
        await client.create_agent(name="Refunds", model="claude-opus-5")


# ── configuration ─────────────────────────────────────────────────────────────


def test_missing_key_raises_at_first_use_not_at_import(monkeypatch: Any) -> None:
    """An eager client would turn an unset key into an import-time crash for the
    whole app. ``ANTHROPIC_API_KEY`` is genuinely unset in this deployment, so
    this is the live path, not a hypothetical."""
    from types import SimpleNamespace

    from app.modules.agents import anthropic_client as module

    # ``Settings`` is frozen, so swap the module's reference rather than a field
    # on it (the same shape ``test_providers.py`` uses).
    monkeypatch.setattr(module, "settings", SimpleNamespace(anthropic_api_key=""))
    client = AnthropicAgentsClient()  # constructing is fine

    with pytest.raises(ConfigurationError, match="ANTHROPIC_API_KEY"):
        client._sdk()
