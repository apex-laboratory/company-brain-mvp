"""Anthropic session-webhook tests (agent-builder-plan §5.3, phase 4).

Signature verification *is* the auth on this route — there is no JWT and no API
key on a vendor callback, and a forged body would move a real session's state. So
most of what is here is about what gets rejected: a missing secret, a stale
timestamp, a tampered body, a wrong key, a signature for a different delivery id.

The route-resolution test is the odd one out and is worth keeping. The sources
module owns ``POST /webhooks/{provider}``, which matches ``/webhooks/anthropic``
perfectly well, and FastAPI resolves in registration order — so a tidy-up that
reordered two lines in ``main.py`` would send every delivery to the
source-integration dispatcher, and nothing else would notice.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.modules.agents.webhook import parse, verify
from app.shared.errors.app_error import UnauthorizedError, ValidationError

_SECRET_RAW = b"0123456789abcdef0123456789abcdef"
_SECRET = "whsec_" + base64.b64encode(_SECRET_RAW).decode()
_DELIVERY_ID = "wh_01"


def _body(event_type: str = "session.status_idled", session_id: str = "ses_1") -> bytes:
    return json.dumps(
        {
            "id": _DELIVERY_ID,
            "type": "event",
            "created_at": "2026-09-04T00:00:00Z",
            "data": {
                "id": session_id,
                "type": event_type,
                "organization_id": "org_1",
                "workspace_id": "anthropic_wrk_1",
            },
        }
    ).encode()


def _headers(
    raw: bytes,
    *,
    delivery_id: str = _DELIVERY_ID,
    timestamp: int | None = None,
    secret: bytes = _SECRET_RAW,
) -> dict[str, str]:
    sent_at = timestamp if timestamp is not None else int(time.time())
    signed = f"{delivery_id}.{sent_at}.".encode() + raw
    digest = hmac.new(secret, signed, hashlib.sha256).digest()
    return {
        "webhook-id": delivery_id,
        "webhook-timestamp": str(sent_at),
        "webhook-signature": "v1," + base64.b64encode(digest).decode(),
    }


def _settings(secret: str) -> Any:
    """``Settings`` is frozen, so the module's reference to it is what gets swapped.

    ``_secret_bytes`` reads ``settings.anthropic_webhook_secret`` off the
    module-level name, so this covers the route tests as well as the direct ones.
    """
    return patch(
        "app.modules.agents.webhook.settings",
        SimpleNamespace(anthropic_webhook_secret=secret),
    )


@pytest.fixture(autouse=True)
def _configured_secret() -> Any:
    with _settings(_SECRET):
        yield


# ── verification ──────────────────────────────────────────────────────────────


def test_a_correctly_signed_delivery_verifies() -> None:
    raw = _body()
    verify(_headers(raw), raw)  # does not raise


def test_a_tampered_body_does_not_verify() -> None:
    raw = _body()
    headers = _headers(raw)

    with pytest.raises(UnauthorizedError):
        verify(headers, raw.replace(b"ses_1", b"ses_2"))


def test_a_signature_for_a_different_delivery_id_does_not_verify() -> None:
    """The id is inside the signed content, so a replay under a new id fails."""
    raw = _body()
    headers = _headers(raw)
    headers["webhook-id"] = "wh_02"

    with pytest.raises(UnauthorizedError):
        verify(headers, raw)


def test_a_wrong_key_does_not_verify() -> None:
    raw = _body()

    with pytest.raises(UnauthorizedError):
        verify(_headers(raw, secret=b"f" * 32), raw)


def test_a_stale_timestamp_is_refused_even_with_a_valid_signature() -> None:
    """Without a replay window, a signature captured once is a valid write forever."""
    raw = _body()

    with pytest.raises(UnauthorizedError, match="window"):
        verify(_headers(raw, timestamp=int(time.time()) - 3600), raw)


def test_a_future_timestamp_is_refused_too() -> None:
    raw = _body()

    with pytest.raises(UnauthorizedError, match="window"):
        verify(_headers(raw, timestamp=int(time.time()) + 3600), raw)


@pytest.mark.parametrize(
    "missing", ["webhook-id", "webhook-timestamp", "webhook-signature"]
)
def test_missing_signature_headers_are_refused(missing: str) -> None:
    raw = _body()
    headers = _headers(raw)
    headers.pop(missing)

    with pytest.raises(UnauthorizedError, match="missing"):
        verify(headers, raw)


def test_an_unset_secret_refuses_everything() -> None:
    """Fail closed. Treating a missing secret as "verification off" is the
    configuration mistake that turns this into an open write endpoint — and an
    empty key is a perfectly usable HMAC key, so it would happily verify a
    forgery signed with the same empty key."""
    raw = _body()
    headers = _headers(raw, secret=b"")

    with _settings(""), pytest.raises(UnauthorizedError, match="not configured"):
        verify(headers, raw)


def test_rotation_works_because_several_signatures_may_be_sent() -> None:
    """Standard Webhooks sends one candidate per live key during a rotation."""
    raw = _body()
    good = _headers(raw)["webhook-signature"]
    headers = _headers(raw, secret=b"f" * 32)
    headers["webhook-signature"] = f"{headers['webhook-signature']} {good}"

    verify(headers, raw)  # does not raise


def test_a_malformed_signature_candidate_is_skipped_not_fatal() -> None:
    raw = _body()
    headers = _headers(raw)
    headers["webhook-signature"] = f"v2,nonsense !!!not-base64!!! {headers['webhook-signature']}"

    verify(headers, raw)


# ── parsing ───────────────────────────────────────────────────────────────────


def test_a_session_event_yields_its_delivery_id_type_and_session() -> None:
    assert parse(_body("session.requires_action", "ses_9")) == (
        _DELIVERY_ID,
        "session.requires_action",
        "ses_9",
    )


@pytest.mark.parametrize(
    "event_type",
    ["vault.created", "vault.credential.refresh_failed", "session_thread.idled"],
)
def test_events_we_have_no_column_for_are_understood_and_dropped(
    event_type: str,
) -> None:
    """Acknowledged, not 4xx'd. An endpoint that retries forever on events it
    ignores eventually starves the ones it handles."""
    assert parse(_body(event_type)) is None


def test_a_body_that_is_not_json_is_a_validation_error() -> None:
    with pytest.raises(ValidationError):
        parse(b"not json at all")


def test_a_body_with_no_data_object_is_dropped_not_fatal() -> None:
    assert parse(json.dumps({"id": "wh_1", "type": "event"}).encode()) is None


# ── the route ─────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def client() -> Any:
    from app.main import app

    app.state.limiter.enabled = False
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
    app.state.limiter.enabled = True


@pytest.mark.asyncio
async def test_the_route_resolves_here_and_not_to_the_source_dispatcher(
    client: AsyncClient,
) -> None:
    """Registration order in main.py is load-bearing. See the module docstring.

    If ``webhooks_router`` won, this body would reach the source-integration
    dispatcher and come back as an unknown provider rather than being enqueued.
    """
    raw = _body()
    with patch(
        "app.modules.agents.router.enqueue", new=AsyncMock()
    ) as enqueued:
        response = await client.post(
            "/api/v1/webhooks/anthropic", content=raw, headers=_headers(raw)
        )

    assert response.status_code == 200
    assert response.json()["data"] == {"status": "accepted"}
    assert enqueued.await_args.args[0] == "sync_agent_session"
    assert enqueued.await_args.args[1:] == ("ses_1", "session.status_idled")


@pytest.mark.asyncio
async def test_the_job_is_deduped_on_anthropics_delivery_id(
    client: AsyncClient,
) -> None:
    raw = _body()
    with patch("app.modules.agents.router.enqueue", new=AsyncMock()) as enqueued:
        await client.post(
            "/api/v1/webhooks/anthropic", content=raw, headers=_headers(raw)
        )

    assert enqueued.await_args.kwargs["_job_id"] == f"agent-session-sync:{_DELIVERY_ID}"


@pytest.mark.asyncio
async def test_an_unverified_delivery_is_401_and_enqueues_nothing(
    client: AsyncClient,
) -> None:
    raw = _body()
    with patch("app.modules.agents.router.enqueue", new=AsyncMock()) as enqueued:
        response = await client.post(
            "/api/v1/webhooks/anthropic",
            content=raw,
            headers=_headers(raw, secret=b"f" * 32),
        )

    assert response.status_code == 401
    enqueued.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_unhandled_event_is_200_and_says_nothing_about_the_session(
    client: AsyncClient,
) -> None:
    """A forged session id must not become an existence oracle, so the response
    body is the same shape whatever the id was."""
    raw = _body("vault.created")
    with patch("app.modules.agents.router.enqueue", new=AsyncMock()) as enqueued:
        response = await client.post(
            "/api/v1/webhooks/anthropic", content=raw, headers=_headers(raw)
        )

    assert response.status_code == 200
    assert response.json()["data"] == {"status": "ignored"}
    enqueued.assert_not_awaited()
