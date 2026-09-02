"""One config-driven OAuth 2.0 authorization-code flow, shared by every connector.

This is the half of the agent builder that has nothing to do with Claude, and it
is where phase 2's cost actually lives (agent-builder-plan §7): four providers,
four consent screens, four token endpoints.

The plan says *no abstraction before the second use* — so read what follows as the
result of that rule, not a violation of it. The providers do not share a class
hierarchy; they share a **specification**. Everything that differs between them is
a field in ``agent_connectors.yaml`` (``token_endpoint_auth``, ``accept_json``,
``extra_authorize_params``, ``scopes``), so adding the fifth provider is a YAML row.
Where a provider needs behaviour no field can express, the honest move is a new
field — not a subclass, and not a branch on the provider's name. There is exactly
one name-free special case below: token responses come back in two encodings and
two error dialects, both of which are read structurally.

**PKCE is always on.** The MCP authorization spec requires it, and providers that
ignore the parameters are unharmed by receiving them. The verifier is *derived*
from the state nonce rather than stored:

    verifier = urlsafe_b64(hmac_sha256("pkce:" + nonce, JWT_ACCESS_SECRET))

which means the callback can reconstruct it from the state it already carries,
with no column added to ``oauth_states`` and nothing extra to expire. It is as
unguessable as the signature on the state itself: the nonce travels through the
browser, but the secret does not, so an attacker holding an intercepted
authorization code still cannot complete the exchange.

**Nothing here writes a token anywhere.** ``exchange_code`` returns a grant the
caller hands to a vault and drops. There is no cache, no retry that re-reads a
response body into a log line, and the exception raised on failure carries the
provider's error *code*, never its payload.
"""
from __future__ import annotations

import base64
import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlencode

import httpx

from app.config.settings import settings
from app.modules.agents.catalog import ConnectorSpec
from app.shared.errors.app_error import AppError, ConfigurationError
from app.shared.helpers.crypto import hmac_sign

log = logging.getLogger(__name__)

# Token endpoints are a handful of small POSTs. Bounded like every other outbound
# call (BACKEND_BEST_PRACTICES §12); generous enough for a slow provider, short
# enough that a hung endpoint does not hold a browser redirect open.
_TIMEOUT = httpx.Timeout(15.0, connect=5.0)


class ConnectorAuthError(AppError):
    """The provider refused the exchange, or answered in a shape we cannot use.

    502, not 400: by the time this is raised the user has already consented, so
    the failure is between us and the provider — a caller retrying the same
    consent may well succeed. The message names the provider and, when the
    provider supplied a machine-readable ``error`` code, that code. The response
    body itself never travels with the exception: it is the one place a token
    could leak into a log or an API envelope.
    """

    def __init__(self, provider: str, code: str | None = None) -> None:
        detail = f" ({code})" if code else ""
        super().__init__(
            502,
            "connector_auth_failed",
            f"Could not complete the {provider} connection{detail}. Try again.",
        )


@dataclass(frozen=True)
class OAuthGrant:
    """What a token endpoint gave back. Held in memory, never persisted."""

    access_token: str
    refresh_token: str | None = None
    expires_at: datetime | None = None
    scope: str | None = None
    # A human label for the connected account, when the provider volunteered one.
    account_label: str | None = None


# ── PKCE ──────────────────────────────────────────────────────────────────────


def code_verifier(nonce: str) -> str:
    """The PKCE verifier for a state nonce. Derived, not stored — see the docstring.

    ``hmac_sign`` returns unpadded urlsafe base64 of a SHA-256 digest: 43
    characters drawn from ``A-Za-z0-9-_``, which satisfies RFC 7636's 43–128
    character ``code_verifier`` grammar exactly.
    """
    return hmac_sign(f"pkce:{nonce}", settings.jwt_access_secret)


def code_challenge(verifier: str) -> str:
    """The S256 challenge for a verifier. ``plain`` is never offered."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


# ── the flow ──────────────────────────────────────────────────────────────────


def authorize_url(spec: ConnectorSpec, *, state: str, redirect_uri: str, nonce: str) -> str:
    """The provider consent URL to send the browser to."""
    if not spec.configured:
        raise ConfigurationError(
            f"The {spec.display_name} connector is not configured on this deployment."
        )
    params: dict[str, str] = {
        "response_type": "code",
        "client_id": spec.client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": code_challenge(code_verifier(nonce)),
        "code_challenge_method": "S256",
    }
    if spec.scopes:
        params["scope"] = spec.scopes
    # Provider-specific query parameters, from the catalog row. Applied last so a
    # deployment can override a default it needs to (Atlassian's `prompt=consent`
    # is the reason this exists), and deliberately *not* able to overwrite the
    # security-bearing ones above.
    for key, value in spec.extra_authorize_params.items():
        params.setdefault(key, value)
    return f"{spec.authorize_endpoint}?{urlencode(params)}"


async def exchange_code(
    spec: ConnectorSpec, *, code: str, redirect_uri: str, nonce: str
) -> OAuthGrant:
    """Trade the authorization code for tokens. Returns them; stores nothing."""
    if not spec.configured:
        raise ConfigurationError(
            f"The {spec.display_name} connector is not configured on this deployment."
        )

    form: dict[str, str] = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": spec.client_id,
        "code_verifier": code_verifier(nonce),
    }
    headers = {"Accept": "application/json"} if spec.accept_json else {}
    extra: dict[str, Any] = {}

    if spec.token_endpoint_auth == "client_secret_basic":
        # The secret goes in the Authorization header, and the client_id stays in
        # the body: RFC 6749 allows both, and providers that key on the body
        # (Notion echoes the flow's owner from it) break without it.
        extra["auth"] = (spec.client_id, spec.client_secret)
    elif spec.token_endpoint_auth == "client_secret_post":
        form["client_secret"] = spec.client_secret

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        try:
            response = await client.post(
                spec.token_endpoint, data=form, headers=headers, **extra
            )
        except httpx.HTTPError as exc:
            log.warning("%s token endpoint unreachable: %s", spec.provider, type(exc).__name__)
            raise ConnectorAuthError(spec.display_name) from exc

    payload = _decode(response)
    error = _error_code(payload)
    if error or response.status_code >= 400:
        # The code, never the body. A token endpoint that 400s often echoes part
        # of the request back, and the request contains the client secret.
        log.warning(
            "%s token exchange failed: status=%s error=%s",
            spec.provider, response.status_code, error or "unknown",
        )
        raise ConnectorAuthError(spec.display_name, error)

    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise ConnectorAuthError(spec.display_name, "no_access_token")

    return OAuthGrant(
        access_token=access_token,
        refresh_token=_string_or_none(payload.get("refresh_token")),
        expires_at=_expires_at(payload.get("expires_in")),
        scope=_string_or_none(payload.get("scope")),
        account_label=_account_label(payload),
    )


def refresh_block(spec: ConnectorSpec, grant: OAuthGrant) -> dict[str, Any] | None:
    """Anthropic's ``refresh`` block, or ``None`` when the grant cannot be refreshed.

    This is what lets us not have a token store: Anthropic runs the
    ``refresh_token`` grant against the provider itself, on its own schedule,
    with a client secret we hand over once. Without it a credential dies at the
    first expiry — and dies *quietly*, surfacing days later as a ``session.error``
    the user reads as "the agent is broken" (§8).

    ``None`` is only correct for a grant with no refresh token, which in practice
    means a provider whose access tokens do not expire. When that is wrong, the
    fix is to ask the provider for offline access in ``scopes``, not to synthesize
    a refresh block here.
    """
    if not grant.refresh_token:
        return None
    block: dict[str, Any] = {
        "client_id": spec.client_id,
        "refresh_token": grant.refresh_token,
        "token_endpoint": spec.token_endpoint,
        "token_endpoint_auth": _token_endpoint_auth(spec),
    }
    # Send back the scope the provider actually granted, not the scope we asked
    # for: a refresh request that widens scope is rejected by spec-compliant
    # providers, and a user may well have unticked something on the consent screen.
    if grant.scope:
        block["scope"] = grant.scope
    return block


def _token_endpoint_auth(spec: ConnectorSpec) -> dict[str, str]:
    if spec.token_endpoint_auth == "none":
        return {"type": "none"}
    return {"type": spec.token_endpoint_auth, "client_secret": spec.client_secret}


# ── response reading ──────────────────────────────────────────────────────────


def _decode(response: httpx.Response) -> dict[str, Any]:
    """Token responses come back JSON or form-encoded. Read whichever arrived.

    Decided by what the body parses as, not by the provider's name: GitHub
    answers form-encoded unless asked for JSON, and "asked for JSON" is a catalog
    field a deployment can get wrong. Falling back keeps that mistake a
    non-event instead of an unparseable success.
    """
    try:
        decoded = response.json()
        if isinstance(decoded, dict):
            return decoded
    except ValueError:
        pass
    try:
        return dict(parse_qsl(response.text, strict_parsing=True))
    except ValueError:
        return {}


def _error_code(payload: dict[str, Any]) -> str | None:
    """The provider's machine-readable failure code, in either dialect.

    OAuth 2.0 says ``{"error": "invalid_grant"}`` with a 4xx. Slack-shaped APIs
    say ``{"ok": false, "error": "..."}`` with a **200**, which is why this is
    checked independently of the status code — trusting the status alone would
    read a refusal as a successful exchange with no token in it.
    """
    if payload.get("ok") is False:
        return _string_or_none(payload.get("error")) or "not_ok"
    error = payload.get("error")
    if isinstance(error, str) and error:
        return error
    # Some providers nest it: {"error": {"code": "..."}}.
    if isinstance(error, dict):
        return _string_or_none(error.get("code")) or "error"
    return None


def _expires_at(expires_in: Any) -> datetime | None:
    try:
        seconds = int(expires_in)
    except (TypeError, ValueError):
        return None
    return datetime.now(UTC) + timedelta(seconds=seconds) if seconds > 0 else None


# Where providers put the name of the account that just consented. Probed in
# order and best-effort: the label is decoration on a card, so a provider that
# volunteers nothing falls back to the catalog's display name rather than
# earning a second API call to go and look it up.
_ACCOUNT_LABEL_KEYS = ("workspace_name", "account_name", "team_name")
_ACCOUNT_LABEL_PATHS = (("team", "name"), ("workspace", "name"), ("account", "name"))


def _account_label(payload: dict[str, Any]) -> str | None:
    for key in _ACCOUNT_LABEL_KEYS:
        label = _string_or_none(payload.get(key))
        if label:
            return label
    for outer, inner in _ACCOUNT_LABEL_PATHS:
        nested = payload.get(outer)
        if isinstance(nested, dict):
            label = _string_or_none(nested.get(inner))
            if label:
                return label
    return None


def _string_or_none(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None
