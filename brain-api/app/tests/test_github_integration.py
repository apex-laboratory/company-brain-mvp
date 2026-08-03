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
from jose import jwt

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
            github_app_client_id="Iv1.testclientid",
            github_app_client_secret="testclientsecret",
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


def _ownership_handler(
    *, user_installations: list[dict], mint_status: int = 201
):
    """Transport handler covering the full exchange flow: user-code exchange,
    /user/installations ownership check, and the installation-token mint."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "github.com" and request.url.path == "/login/oauth/access_token":
            return httpx.Response(200, json={"access_token": "ghu_usertoken"})
        if request.url.path == "/user/installations":
            assert request.headers["Authorization"] == "Bearer ghu_usertoken"
            return httpx.Response(200, json={"installations": user_installations})
        assert request.url.path == "/app/installations/inst-1/access_tokens"
        assert request.headers["Authorization"].startswith("Bearer ")
        return httpx.Response(
            mint_status,
            json={"token": "ghs_installtoken", "expires_at": "2026-06-20T10:00:00Z"},
        )

    return handler


async def test_exchange_code_mints_installation_token(
    github: GitHubIntegration, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_transport(
        monkeypatch, _ownership_handler(user_installations=[{"id": "inst-1"}])
    )
    tokens = await github.exchange_code("code123", "cb", installation_id="inst-1")
    assert tokens.access_token == "ghs_installtoken"
    # The installation_id is stored as the re-mint credential and the account id.
    assert tokens.refresh_token == "inst-1"
    assert tokens.external_account_id == "inst-1"
    assert tokens.expires_at is not None


async def test_exchange_code_rejects_foreign_installation(
    github: GitHubIntegration, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The authorizing user can see only their own installation; naming a victim's
    # installation_id must be rejected (cross-tenant takeover guard).
    _install_transport(
        monkeypatch, _ownership_handler(user_installations=[{"id": "someone-elses"}])
    )
    with pytest.raises(base.ConnectorAuthError):
        await github.exchange_code("code123", "cb", installation_id="inst-1")


async def test_exchange_code_requires_user_code(
    github: GitHubIntegration, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No user-authorization code → ownership can't be proven → reject.
    _install_transport(
        monkeypatch, _ownership_handler(user_installations=[{"id": "inst-1"}])
    )
    with pytest.raises(base.ConnectorAuthError):
        await github.exchange_code("", "cb", installation_id="inst-1")


async def test_exchange_code_requires_oauth_client_config(
    github: GitHubIntegration, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Without the App OAuth client configured, ownership can't be verified — the
    # connect must fail closed rather than silently skip the check.
    monkeypatch.setattr(github_module.settings, "github_app_client_id", "")
    _install_transport(
        monkeypatch, _ownership_handler(user_installations=[{"id": "inst-1"}])
    )
    with pytest.raises(base.ConnectorAuthError):
        await github.exchange_code("code123", "cb", installation_id="inst-1")


async def test_refresh_skips_ownership_check(
    github: GitHubIntegration, monkeypatch: pytest.MonkeyPatch
) -> None:
    # refresh() re-mints from our own stored installation_id — no user code exists,
    # and ownership was proven at connect time, so it must not hit the user endpoints.
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/app/installations/inst-1/access_tokens"
        return httpx.Response(
            201, json={"token": "ghs_fresh", "expires_at": "2026-06-20T10:00:00Z"}
        )

    _install_transport(monkeypatch, handler)
    tokens = await github.refresh("inst-1")
    assert tokens.access_token == "ghs_fresh"
    assert tokens.refresh_token == "inst-1"


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


def _two_repo_handler(good_issues: list[dict], bad_status: int):
    """Transport handler: ``acme/good`` returns issues, ``acme/bad`` returns an error."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/installation/repositories":
            return httpx.Response(
                200,
                json={"repositories": [{"full_name": "acme/good"}, {"full_name": "acme/bad"}]},
            )
        if request.url.path == "/repos/acme/good/issues":
            return httpx.Response(200, json=good_issues)
        return httpx.Response(bad_status, json={"message": "nope"})

    return handler


async def test_fetch_since_skips_permanent_repo_error_and_advances(
    github: GitHubIntegration, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A 410 (issues disabled) on one repo is skipped permanently; the good repo's
    # items still come through and the cursor advances.
    issues = [_issue(2, "2026-06-12T10:00:00Z", "Good")]
    _install_transport(monkeypatch, _two_repo_handler(issues, 410))
    channel = base.ChannelRef(external_id="inst-1", name="workspace")
    items, next_cursor = await github.fetch_since("tok", channel, None)
    assert {i.external_id for i in items} == {"2"}
    assert next_cursor == "2026-06-12T10:00:00+00:00"


async def test_fetch_since_holds_cursor_on_transient_repo_error(
    github: GitHubIntegration, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A 500 on one repo is transient: good items still ingest, but the cursor is
    # held at its prior value so the next sync retries the failed repo.
    issues = [_issue(2, "2026-06-12T10:00:00Z", "Good")]
    _install_transport(monkeypatch, _two_repo_handler(issues, 500))
    channel = base.ChannelRef(external_id="inst-1", name="workspace")
    prior = "2026-06-01T00:00:00+00:00"
    items, next_cursor = await github.fetch_since("tok", channel, prior)
    assert {i.external_id for i in items} == {"2"}
    assert next_cursor == prior  # held, not advanced


async def test_fetch_since_reraises_on_401(
    github: GitHubIntegration, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A 401 is a whole-connection auth failure — it must propagate so source_sync
    # can mark the connection in error.
    _install_transport(monkeypatch, _two_repo_handler([], 401))
    channel = base.ChannelRef(external_id="inst-1", name="workspace")
    with pytest.raises(httpx.HTTPStatusError):
        await github.fetch_since("tok", channel, None)


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


def test_normalize_maps_issue_comment_to_the_comment(github: GitHubIntegration) -> None:
    # An issue_comment delivery carries BOTH the comment and its parent issue; the
    # comment must win or its text/author are silently replaced by the issue's.
    envelope = base.RawItem(
        external_id="",
        payload={
            "action": "created",
            "issue": _issue(5, "2026-06-14T10:00:00Z", "Parent issue"),
            "comment": {
                "id": 900,
                "body": "the actual comment text",
                "html_url": "https://github.com/acme/repo/issues/5#issuecomment-900",
                "updated_at": "2026-06-14T11:00:00Z",
                "user": {"id": 8, "login": "commenter"},
            },
            "installation": {"id": 42},
        },
    )
    event = github.normalize(envelope)
    assert event.event_type == "comment"
    assert event.source_id == "900"
    assert "the actual comment text" in event.content
    assert event.actor["name"] == "commenter"


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


# ── uninstall (disconnect removes the App from the user's repos) ──────────────
def _uninstall_handler(status: int, seen: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["auth"] = request.headers["Authorization"]
        return httpx.Response(status, json={} if status < 300 else {"message": "nope"})

    return handler


async def test_uninstall_deletes_the_installation_with_an_app_jwt(
    github: GitHubIntegration, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Must be the App JWT, not an installation token: the endpoint acts for the App
    # itself, and the installation token is exactly what's being destroyed.
    seen: dict = {}
    _install_transport(monkeypatch, _uninstall_handler(204, seen))

    await github.uninstall("inst-1")

    assert seen["method"] == "DELETE"
    assert seen["path"] == "/app/installations/inst-1"
    token = seen["auth"].removeprefix("Bearer ")
    claims = jwt.get_unverified_claims(token)
    assert claims["iss"] == "123456"  # App id ⇒ App JWT, not ghs_/ghu_ token


async def test_uninstall_treats_404_as_already_gone(
    github: GitHubIntegration, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Removed from GitHub's own settings page first — that's the desired end state,
    # so it must not raise and get logged as a failure.
    _install_transport(monkeypatch, _uninstall_handler(404, {}))
    await github.uninstall("inst-1")


async def test_uninstall_raises_on_a_real_failure(
    github: GitHubIntegration, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The service swallows this, but the connector must still report it so the
    # "may need removing manually" log line actually fires.
    _install_transport(monkeypatch, _uninstall_handler(500, {}))
    with pytest.raises(httpx.HTTPStatusError):
        await github.uninstall("inst-1")
