"""Normalize raw GitHub REST payloads into canonical `raw_content` rows.

Contract: airbyte/raw-content-contract.md (GitHub section).

Each function takes a single raw GitHub JSON dict plus the `owner/repo` slug
and returns a `RawContentRow` dataclass that the writer can upsert.

These functions are pure — no I/O, no logging — so they're easy to unit test.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


SOURCE = "github"


@dataclass
class RawContentRow:
    source: str
    source_id: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------- helpers ----------

def _split_repo(repo: str) -> tuple[str, str]:
    if "/" not in repo:
        raise ValueError(f"Expected 'owner/repo', got {repo!r}")
    owner, name = repo.split("/", 1)
    return owner, name


def _author(user: dict[str, Any] | None) -> dict[str, Any]:
    if not user:
        return {"id": None, "name": None, "email": None, "handle": None}
    return {
        "id": str(user.get("id")) if user.get("id") is not None else None,
        "name": user.get("name"),
        "email": user.get("email"),
        "handle": user.get("login"),
    }


def _labels(raw_labels: list[Any] | None) -> list[str]:
    if not raw_labels:
        return []
    out: list[str] = []
    for lbl in raw_labels:
        if isinstance(lbl, dict):
            name = lbl.get("name")
            if name:
                out.append(str(name))
        elif isinstance(lbl, str):
            out.append(lbl)
    return out


def _assignees(raw: list[dict[str, Any]] | None) -> list[str]:
    if not raw:
        return []
    return [a.get("login") for a in raw if a.get("login")]


def _strip_html_comments(body: str | None) -> str:
    """Remove GitHub's hidden HTML comments (e.g. PR template instructions)."""
    if not body:
        return ""
    return re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL).strip()


def _join_title_body(title: str | None, body: str | None) -> str:
    title = (title or "").strip()
    body = _strip_html_comments(body)
    if title and body:
        return f"{title}\n\n{body}"
    return title or body


def _issue_number_from_url(url: str | None) -> int | None:
    """Extract '42' from '.../issues/42' or '.../pulls/42' (pr review comments
    expose `pull_request_url`, not the number directly)."""
    if not url:
        return None
    m = re.search(r"/(?:issues|pulls)/(\d+)", url)
    return int(m.group(1)) if m else None


# ---------- normalizers ----------

def normalize_issue(repo: str, issue: dict[str, Any]) -> RawContentRow:
    """`/repos/{repo}/issues` item (we filter PRs out upstream)."""
    owner, name = _split_repo(repo)
    number = issue["number"]
    title = issue.get("title")
    body = issue.get("body")
    metadata = {
        "entity_type": "issue",
        "record_url": issue.get("html_url"),
        "title": title,
        "author": _author(issue.get("user")),
        "created_at": issue.get("created_at"),
        "updated_at": issue.get("updated_at"),
        "tags": _labels(issue.get("labels")),
        "airbyte_stream": "issues",
        "repo": name,
        "org": owner,
        "issue_number": number,
        "state": issue.get("state"),
        "state_reason": issue.get("state_reason"),
        "labels": _labels(issue.get("labels")),
        "assignees": _assignees(issue.get("assignees")),
        "milestone": (issue.get("milestone") or {}).get("title"),
        "comments_count": issue.get("comments"),
        "closed_at": issue.get("closed_at"),
        "raw_id": issue.get("id"),
    }
    return RawContentRow(
        source=SOURCE,
        source_id=f"github_issue:{name}:{number}",
        content=_join_title_body(title, body),
        metadata=metadata,
    )


def normalize_pull_request(repo: str, pr: dict[str, Any]) -> RawContentRow:
    owner, name = _split_repo(repo)
    number = pr["number"]
    title = pr.get("title")
    body = pr.get("body")
    base = pr.get("base") or {}
    head = pr.get("head") or {}
    metadata = {
        "entity_type": "pull_request",
        "record_url": pr.get("html_url"),
        "title": title,
        "author": _author(pr.get("user")),
        "created_at": pr.get("created_at"),
        "updated_at": pr.get("updated_at"),
        "tags": _labels(pr.get("labels")),
        "airbyte_stream": "pull_requests",
        "repo": name,
        "org": owner,
        "pr_number": number,
        "state": pr.get("state"),
        "labels": _labels(pr.get("labels")),
        "assignees": _assignees(pr.get("assignees")),
        "requested_reviewers": _assignees(pr.get("requested_reviewers")),
        "base_branch": base.get("ref"),
        "head_branch": head.get("ref"),
        "merged": bool(pr.get("merged_at")),
        "merged_at": pr.get("merged_at"),
        "merge_commit_sha": pr.get("merge_commit_sha"),
        "draft": pr.get("draft"),
        "closed_at": pr.get("closed_at"),
        "raw_id": pr.get("id"),
    }
    return RawContentRow(
        source=SOURCE,
        source_id=f"github_pr:{name}:{number}",
        content=_join_title_body(title, body),
        metadata=metadata,
    )


def normalize_issue_comment(repo: str, comment: dict[str, Any]) -> RawContentRow:
    """Issue comments include both real-issue and PR-conversation comments
    (PR conversation tab posts come back through the same endpoint).  GitHub
    issues and PRs share a number space, so we have to detect which kind of
    parent the comment belongs to before we can set `parent_source_id` to a
    value that will actually resolve to a row we've ingested."""
    owner, name = _split_repo(repo)
    issue_url = comment.get("issue_url") or ""
    parent_number = _issue_number_from_url(issue_url)
    html_url = comment.get("html_url") or ""
    # `html_url` is /pull/<n>#... for PR conversation comments and
    # /issues/<n>#... for true issue comments. Use that to pick the parent.
    parent_is_pr = "/pull/" in html_url
    parent_entity_type = "pull_request" if parent_is_pr else "issue"
    parent_source_id = (
        (f"github_pr:{name}:{parent_number}" if parent_is_pr
         else f"github_issue:{name}:{parent_number}")
        if parent_number else None
    )
    comment_id = comment["id"]
    body = comment.get("body") or ""
    metadata = {
        "entity_type": "issue_comment",
        "record_url": html_url,
        "title": None,
        "author": _author(comment.get("user")),
        "created_at": comment.get("created_at"),
        "updated_at": comment.get("updated_at"),
        "tags": [],
        "airbyte_stream": "issue_comments",
        "repo": name,
        "org": owner,
        "issue_number": None if parent_is_pr else parent_number,
        "pr_number": parent_number if parent_is_pr else None,
        "comment_id": comment_id,
        "parent_entity_type": parent_entity_type,
        "parent_entity_id": parent_number,
        "parent_source_id": parent_source_id,
        "issue_url": issue_url,
        "author_association": comment.get("author_association"),
        "raw_id": comment.get("id"),
    }
    return RawContentRow(
        source=SOURCE,
        source_id=f"github_issue_comment:{name}:{parent_number}:{comment_id}",
        content=_strip_html_comments(body),
        metadata=metadata,
    )


def normalize_pr_review_comment(repo: str, comment: dict[str, Any]) -> RawContentRow:
    """Inline review comments left on PR diffs."""
    owner, name = _split_repo(repo)
    pr_url = comment.get("pull_request_url") or ""
    pr_number = _issue_number_from_url(pr_url)
    comment_id = comment["id"]
    body = comment.get("body") or ""
    metadata = {
        "entity_type": "pr_review_comment",
        "record_url": comment.get("html_url"),
        "title": None,
        "author": _author(comment.get("user")),
        "created_at": comment.get("created_at"),
        "updated_at": comment.get("updated_at"),
        "tags": [],
        "airbyte_stream": "pull_request_comments",
        "repo": name,
        "org": owner,
        "pr_number": pr_number,
        "comment_id": comment_id,
        "review_id": comment.get("pull_request_review_id"),
        "in_reply_to_id": comment.get("in_reply_to_id"),
        "parent_entity_type": "pull_request",
        "parent_entity_id": pr_number,
        "parent_source_id": (
            f"github_pr:{name}:{pr_number}" if pr_number else None
        ),
        "path": comment.get("path"),
        "line": comment.get("line") or comment.get("original_line"),
        "side": comment.get("side"),
        "commit_id": comment.get("commit_id"),
        "author_association": comment.get("author_association"),
        "raw_id": comment.get("id"),
    }
    return RawContentRow(
        source=SOURCE,
        source_id=f"github_pr_review_comment:{name}:{pr_number}:{comment_id}",
        content=_strip_html_comments(body),
        metadata=metadata,
    )
