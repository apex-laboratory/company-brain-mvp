"""Unit tests for the context expanders — each mocks its module's http_client."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.pipeline.expanders import get_expander, needs_expansion
from app.pipeline.expanders.base import ExpandRequest


def _req(
    provider: str, payload: dict, *, token="tok", account_id=None, content="original"
) -> ExpandRequest:
    return ExpandRequest(
        provider=provider, token=token, account_id=account_id, payload=payload, content=content
    )


def _mock_http(module: str, json_payload: dict, *, status=200):
    """Patch ``<module>.http_client`` so .get returns ``json_payload``."""
    resp = MagicMock()
    resp.json.return_value = json_payload
    resp.raise_for_status = MagicMock()
    if status >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "err", request=httpx.Request("GET", "https://x"),
            response=httpx.Response(status),
        )
    client = AsyncMock()
    client.get.return_value = resp
    return patch(f"app.pipeline.expanders.{module}.http_client", return_value=client), client


# ── registry ──────────────────────────────────────────────────────────────────

def test_registry_membership() -> None:
    for p in ("slack", "notion", "github", "zendesk", "jira", "google_drive"):
        assert needs_expansion(p)
    assert not needs_expansion("gmail")


def test_unregistered_provider_raises() -> None:
    # Callers gate on needs_expansion; asking for an unregistered expander is a bug.
    assert not needs_expansion("gmail")
    with pytest.raises(KeyError):
        get_expander("gmail")


# ── slack ─────────────────────────────────────────────────────────────────────

async def test_slack_joins_thread_replies() -> None:
    payload = {"channel": "C1", "ts": "1.0", "thread_ts": "1.0"}
    replies = {"ok": True, "messages": [
        {"user": "U1", "text": "root", "reactions": [{"name": "+1", "count": 2}]},
        {"user": "U2", "text": "reply"},
    ]}
    patcher, client = _mock_http("slack", replies)
    with patcher:
        out = await get_expander("slack").expand(_req("slack", payload))
    assert "U1: root" in out.text and "U2: reply" in out.text
    assert out.reactions == [{"name": "+1", "count": 2}]
    assert client.get.await_args.kwargs["params"]["channel"] == "C1"


async def test_slack_missing_channel_falls_back() -> None:
    patcher, client = _mock_http("slack", {"ok": True, "messages": []})
    with patcher:
        out = await get_expander("slack").expand(_req("slack", {"text": "x"}, content="orig"))
    assert out.text == "orig"
    client.get.assert_not_awaited()  # no ids → no API call


async def test_slack_ok_false_falls_back_to_single_message() -> None:
    payload = {"channel": "C1", "ts": "1.0"}
    patcher, _ = _mock_http("slack", {"ok": False, "error": "thread_not_found"})
    with patcher:
        out = await get_expander("slack").expand(_req("slack", payload, content="orig"))
    assert out.text == "orig"


# ── notion ────────────────────────────────────────────────────────────────────

async def test_notion_flattens_block_children() -> None:
    para = {"rich_text": [{"plain_text": "Hello "}, {"plain_text": "world"}]}
    blocks = {"results": [
        {"type": "paragraph", "paragraph": para, "has_children": False},
        {"type": "heading_1", "heading_1": {"rich_text": [{"plain_text": "Policy"}]},
         "has_children": False},
    ]}
    patcher, _ = _mock_http("notion", blocks)
    with patcher:
        out = await get_expander("notion").expand(_req("notion", {"id": "p1"}, content="Title"))
    assert "Hello world" in out.text and "Policy" in out.text and out.text.startswith("Title")


async def test_notion_missing_id_falls_back() -> None:
    out = await get_expander("notion").expand(_req("notion", {}, content="orig"))
    assert out.text == "orig"


# ── github ────────────────────────────────────────────────────────────────────

async def test_github_appends_comments() -> None:
    payload = {"issue": {"comments_url": "https://api.github.com/…/comments", "html_url": "h"}}
    comments = [{"user": {"login": "alice"}, "body": "ship it"}]
    patcher, _ = _mock_http("github", comments)
    with patcher:
        out = await get_expander("github").expand(_req("github", payload, content="PR body"))
    assert out.text.startswith("PR body") and "alice: ship it" in out.text
    assert out.url == "h"


async def test_github_no_comments_url_falls_back() -> None:
    out = await get_expander("github").expand(_req("github", {"foo": 1}, content="orig"))
    assert out.text == "orig"


# ── zendesk ───────────────────────────────────────────────────────────────────

async def test_zendesk_appends_ticket_comments() -> None:
    payload = {"id": 42, "_subdomain": "acme", "tags": ["refund"]}
    body = {"comments": [{"author_id": 7, "plain_body": "resolved via refund"}]}
    patcher, client = _mock_http("zendesk", body)
    with patcher:
        out = await get_expander("zendesk").expand(_req("zendesk", payload, content="subject"))
    assert "resolved via refund" in out.text
    assert out.metadata["tags"] == ["refund"]
    assert "acme.zendesk.com/api/v2/tickets/42" in client.get.await_args.args[0]


async def test_zendesk_missing_subdomain_falls_back() -> None:
    out = await get_expander("zendesk").expand(_req("zendesk", {"id": 1}, content="orig"))
    assert out.text == "orig"


# ── jira ──────────────────────────────────────────────────────────────────────

async def test_jira_appends_adf_comments() -> None:
    payload = {"key": "OPS-1", "_site_url": "https://acme.atlassian.net"}
    adf = {"type": "doc", "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": "use the runbook"}]}
    ]}
    body = {"comments": [{"author": {"displayName": "Bob"}, "body": adf}]}
    patcher, client = _mock_http("jira", body)
    with patcher:
        out = await get_expander("jira").expand(
            _req("jira", payload, account_id="cloud-123", content="summary")
        )
    assert "Bob: use the runbook" in out.text
    assert "ex/jira/cloud-123/rest/api/3/issue/OPS-1/comment" in client.get.await_args.args[0]


async def test_jira_missing_cloud_id_falls_back() -> None:
    out = await get_expander("jira").expand(
        _req("jira", {"key": "OPS-1"}, account_id=None, content="orig")
    )
    assert out.text == "orig"


# ── google drive ──────────────────────────────────────────────────────────────

async def test_drive_appends_comments() -> None:
    body = {"comments": [{"author": {"displayName": "Cara"}, "content": "LGTM"}]}
    patcher, _ = _mock_http("google_drive", body)
    with patcher:
        out = await get_expander("google_drive").expand(
            _req("google_drive", {"id": "f1"}, content="doc text")
        )
    assert out.text.startswith("doc text") and "Cara: LGTM" in out.text


async def test_drive_no_comments_returns_content() -> None:
    patcher, _ = _mock_http("google_drive", {"comments": []})
    with patcher:
        out = await get_expander("google_drive").expand(
            _req("google_drive", {"id": "f1"}, content="doc text")
        )
    assert out.text == "doc text"
