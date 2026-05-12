"""High-level orchestrator: fetch GitHub → normalize → upsert into raw_content."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from services.github_client import GitHubAuthError, GitHubClient
from services.github_normalizer import (
    RawContentRow,
    normalize_issue,
    normalize_issue_comment,
    normalize_pr_review_comment,
    normalize_pull_request,
)
from services.raw_content_writer import upsert_rows

logger = logging.getLogger(__name__)


@dataclass
class StreamCounts:
    fetched: int = 0
    inserted: int = 0
    updated: int = 0


@dataclass
class RepoSyncResult:
    repo: str
    streams: dict[str, StreamCounts] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "streams": {
                name: {
                    "fetched": s.fetched,
                    "inserted": s.inserted,
                    "updated": s.updated,
                }
                for name, s in self.streams.items()
            },
            "errors": self.errors,
        }


async def sync_repo(
    repo: str,
    *,
    state: str = "all",
    since: str | None = None,
    batch_size: int = 200,
    token: str | None = None,
) -> RepoSyncResult:
    """Pull issues, PRs, issue comments, and PR review comments for one repo.

    Idempotent — running it twice over the same window only upserts.
    `since` is forwarded to GitHub for issues and comments (ISO-8601 string).
    """
    result = RepoSyncResult(repo=repo)

    async with GitHubClient(token=token) as gh:
        await _drain(
            stream_name="issues",
            iterator=gh.iter_issues(repo, state=state, since=since),
            normalize=lambda item: normalize_issue(repo, item),
            result=result,
            batch_size=batch_size,
        )
        await _drain(
            stream_name="pull_requests",
            iterator=gh.iter_pull_requests(repo, state=state),
            normalize=lambda item: normalize_pull_request(repo, item),
            result=result,
            batch_size=batch_size,
        )
        await _drain(
            stream_name="issue_comments",
            iterator=gh.iter_issue_comments(repo, since=since),
            normalize=lambda item: normalize_issue_comment(repo, item),
            result=result,
            batch_size=batch_size,
        )
        await _drain(
            stream_name="pr_review_comments",
            iterator=gh.iter_pr_review_comments(repo, since=since),
            normalize=lambda item: normalize_pr_review_comment(repo, item),
            result=result,
            batch_size=batch_size,
        )

    return result


async def sync_repos(
    repos: list[str],
    *,
    state: str = "all",
    since: str | None = None,
    token: str | None = None,
) -> list[RepoSyncResult]:
    if not repos:
        return []
    out: list[RepoSyncResult] = []
    for repo in repos:
        try:
            out.append(await sync_repo(repo, state=state, since=since, token=token))
        except GitHubAuthError as e:
            logger.error("Auth failure for %s: %s", repo, e)
            failed = RepoSyncResult(repo=repo)
            failed.errors.append(f"auth: {e}")
            out.append(failed)
        except Exception as e:  # noqa: BLE001 — surface, don't crash other repos
            logger.exception("Sync failed for %s", repo)
            failed = RepoSyncResult(repo=repo)
            failed.errors.append(f"{type(e).__name__}: {e}")
            out.append(failed)
    return out


async def _drain(
    *,
    stream_name: str,
    iterator,
    normalize,
    result: RepoSyncResult,
    batch_size: int,
) -> None:
    counts = result.streams.setdefault(stream_name, StreamCounts())
    buffer: list[RawContentRow] = []
    try:
        async for raw in iterator:
            try:
                buffer.append(normalize(raw))
            except Exception as e:  # noqa: BLE001 — skip bad rows but keep going
                logger.warning(
                    "Failed to normalize %s row from %s: %s", stream_name, result.repo, e
                )
                continue
            counts.fetched += 1
            if len(buffer) >= batch_size:
                ins, upd = await upsert_rows(buffer)
                counts.inserted += ins
                counts.updated += upd
                buffer.clear()
        if buffer:
            ins, upd = await upsert_rows(buffer)
            counts.inserted += ins
            counts.updated += upd
            buffer.clear()
    except Exception as e:  # noqa: BLE001
        logger.exception("Stream %s failed for %s", stream_name, result.repo)
        result.errors.append(f"{stream_name}: {type(e).__name__}: {e}")
