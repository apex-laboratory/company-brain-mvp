"""GitHub integration unit tests (KAN-7).

Outbound HTTP is intercepted with ``httpx.MockTransport`` swapped into the shared
client — no network. A throwaway RSA keypair is generated so the App-JWT signing
path runs for real (the mock token endpoint doesn't validate it).
"""
from __future__ import annotations

import hashlib
import hmac
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.integrations import base
from app.integrations import github as github_module
from app.integrations.github import GitHubIntegration


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> GitHubIntegration:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    # The real settings singleton is frozen; swap the module reference for a stub
    # carrying just the GitHub App fields the integration reads.
    monkeypatch.setattr(
        github_module,
        "settings",
        SimpleNamespace(
            github_app_id="123456",
            github_app_private_key=pem,
            github_app_slug="brainite-app",
        ),
    )
    return GitHubIntegration()


def _install_transport(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(base, "_client", client)


def test_authorize_url_points_at_install_page(github: GitHubIntegration) -> None:
    url = github.authorize_url("STATE123", "https://api.example.com/cb")
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    assert parsed.netloc == "github.com"
    assert parsed.path == "/apps/brainite-app/installations/new"
    assert qs["state"] == ["STATE123"]


async def test_exchange_code_mints_installation_token(
    github: GitHubIntegration, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/app/installations/inst-1/access_tokens"
        assert request.headers["Authorization"].startswith("Bearer ")
        return httpx.Response(
            201,
            json={"token": "ghs_installtoken", "expires_at": "2026-06-20T10:00:00Z"},
        )

    _install_transport(monkeypatch, handler)
    tokens = await github.exchange_code("", "cb", installation_id="inst-1")
    assert tokens.access_token == "ghs_installtoken"
    # The installation_id is stored as the re-mint credential and the account id.
    assert tokens.refresh_token == "inst-1"
    assert tokens.external_account_id == "inst-1"
    assert tokens.expires_at is not None


async def test_exchange_code_requires_installation_id(github: GitHubIntegration) -> None:
    with pytest.raises(ValueError):
        await github.exchange_code("code", "cb", installation_id=None)


def _issue(issue_id: int, updated: str, title: str, *, pr: bool = False) -> dict:
    obj = {
        "id": issue_id,
        "number": issue_id,
        "title": title,
        "body": "body text",
        "html_url": f"https://github.com/acme/repo/issues/{issue_id}",
        "updated_at": updated,
        "created_at": updated,
        "user": {"id": 7, "login": "octocat"},
    }
    if pr:
        obj["pull_request"] = {"url": "https://api.github.com/..."}
    return obj


async def test_fetch_since_enumerates_repos_and_advances_cursor(
    github: GitHubIntegration, monkeypatch: pytest.MonkeyPatch
) -> None:
    issues = [
        _issue(3, "2026-06-13T10:00:00Z", "Newest", pr=True),
        _issue(2, "2026-06-12T10:00:00Z", "Middle"),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/installation/repositories":
            return httpx.Response(
                200, json={"repositories": [{"full_name": "acme/repo"}]}
            )
        assert request.url.path == "/repos/acme/repo/issues"
        # `since` cursor is forwarded to GitHub.
        assert request.url.params.get("since") == "2026-06-11T10:00:00+00:00"
        return httpx.Response(200, json=issues)

    _install_transport(monkeypatch, handler)
    channel = base.ChannelRef(external_id="inst-1", name="workspace")
    items, next_cursor = await github.fetch_since(
        "tok", channel, "2026-06-11T10:00:00+00:00"
    )
    assert {i.external_id for i in items} == {"2", "3"}
    assert next_cursor == "2026-06-13T10:00:00+00:00"


def test_normalize_maps_bare_issue(github: GitHubIntegration) -> None:
    item = base.RawItem(external_id="2", payload=_issue(2, "2026-06-12T10:00:00Z", "Hi"))
    event = github.normalize(item)
    assert event.provider == "github"
    assert event.source_id == "2"
    assert event.event_type == "issue"
    assert event.external_event_id == "2:2026-06-12T10:00:00Z"
    assert event.content.startswith("Hi")
    assert event.actor["name"] == "octocat"


def test_normalize_maps_pr_and_webhook_envelope(github: GitHubIntegration) -> None:
    # A bare issue carrying a pull_request *stub* is classified as a PR, and must
    # keep its own id/updated_at — not the stub's (regression: external_event_id was
    # "None:None" when the stub was mistaken for the core object).
    pr_item = base.RawItem(
        external_id="3", payload=_issue(3, "2026-06-13T10:00:00Z", "PR", pr=True)
    )
    pr_event = github.normalize(pr_item)
    assert pr_event.event_type == "pr"
    assert pr_event.source_id == "3"
    assert pr_event.external_event_id == "3:2026-06-13T10:00:00Z"

    # A webhook envelope is unwrapped to its core object.
    envelope = base.RawItem(
        external_id="",
        payload={
            "action": "opened",
            "issue": _issue(5, "2026-06-14T10:00:00Z", "Webhooked"),
            "repository": {"full_name": "acme/repo"},
            "sender": {"id": 9, "login": "sender-login"},
            "installation": {"id": 42},
        },
    )
    event = github.normalize(envelope)
    assert event.source_id == "5"
    assert event.event_type == "issue"
    assert event.url.endswith("/issues/5")


def test_normalize_rejects_non_content_payload(github: GitHubIntegration) -> None:
    # A `ping` delivery has no issue/PR/comment — normalize raises so ingest skips.
    ping = base.RawItem(external_id="", payload={"zen": "Keep it simple", "hook_id": 1})
    with pytest.raises(ValueError):
        github.normalize(ping)


def test_verify_webhook_accepts_valid_signature(github: GitHubIntegration) -> None:
    secret = "shhh"
    body = b'{"action":"opened"}'
    digest = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert github.verify_webhook({"X-Hub-Signature-256": digest}, body, secret) is True
    # Case-insensitive header lookup.
    assert github.verify_webhook({"x-hub-signature-256": digest}, body, secret) is True


def test_verify_webhook_rejects_bad_or_missing_signature(github: GitHubIntegration) -> None:
    body = b'{"action":"opened"}'
    assert github.verify_webhook({"X-Hub-Signature-256": "sha256=bad"}, body, "shhh") is False
    assert github.verify_webhook({}, body, "shhh") is False
    # No secret configured → reject.
    assert github.verify_webhook({"X-Hub-Signature-256": "x"}, body, "") is False
