"""Async GitHub REST client.

MVP scope: pulls issues, pull requests, issue comments, and PR review comments
for a list of `owner/repo` slugs.  Uses cursor-style page traversal via the
`Link: rel="next"` header so we don't have to know the total page count.

This module deliberately stays transport-only: it returns raw GitHub JSON dicts
and does not normalize.  Normalization lives in `services/github_normalizer`.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from config import settings

logger = logging.getLogger(__name__)


class GitHubAuthError(RuntimeError):
    """Raised when GitHub returns 401/403 due to a missing or invalid token."""


class GitHubClient:
    """Thin async wrapper around the GitHub REST v3 API.

    Use as an async context manager so the underlying httpx.AsyncClient is
    closed deterministically:

        async with GitHubClient() as gh:
            async for issue in gh.iter_issues("octocat/Hello-World"):
                ...
    """

    def __init__(
        self,
        token: str | None = None,
        base_url: str | None = None,
        per_page: int | None = None,
        max_pages: int | None = None,
        timeout: float | None = None,
    ) -> None:
        self.token = token if token is not None else settings.github_token
        self.base_url = (base_url or settings.github_api_url).rstrip("/")
        self.per_page = per_page or settings.github_per_page
        self.max_pages = max_pages or settings.github_max_pages
        self.timeout = timeout or settings.github_request_timeout
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "GitHubClient":
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "company-brain-mvp/github-connector",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=self.timeout,
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ---------- low-level paged GET ----------

    async def _paged_get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield items across all pages, following `Link: rel="next"`."""
        if self._client is None:
            raise RuntimeError("GitHubClient must be used as an async context manager")

        merged: dict[str, Any] = {"per_page": self.per_page}
        if params:
            merged.update(params)

        url: str | None = path
        request_params: dict[str, Any] | None = merged
        pages_seen = 0

        while url is not None:
            resp = await self._request_with_retry(url, request_params)
            pages_seen += 1
            payload = resp.json()
            if isinstance(payload, list):
                for item in payload:
                    yield item
            elif isinstance(payload, dict):
                # Some endpoints return objects; treat as a single item for caller.
                yield payload
            else:
                logger.warning("Unexpected payload type from %s: %r", url, type(payload))

            if pages_seen >= self.max_pages:
                logger.warning(
                    "Hit GITHUB_MAX_PAGES=%s on %s — stopping pagination early",
                    self.max_pages,
                    path,
                )
                break

            url = _next_link(resp.headers.get("Link"))
            request_params = None  # Subsequent links already carry the cursor params.

    async def _request_with_retry(
        self,
        url: str,
        params: dict[str, Any] | None,
        max_retries: int = 5,
    ) -> httpx.Response:
        """GET with primary-rate-limit + transient-5xx retry."""
        assert self._client is not None
        attempt = 0
        while True:
            resp = await self._client.get(url, params=params)
            if resp.status_code in (401, 403) and _is_auth_failure(resp):
                raise GitHubAuthError(
                    f"GitHub auth failed ({resp.status_code}) on {url}: {resp.text[:200]}"
                )
            if resp.status_code == 403 and _is_rate_limited(resp):
                wait_s = _retry_after_seconds(resp, default=30)
                logger.warning(
                    "GitHub rate-limited on %s; sleeping %.1fs (attempt %s)",
                    url,
                    wait_s,
                    attempt + 1,
                )
                await asyncio.sleep(wait_s)
            elif resp.status_code in (429, 500, 502, 503, 504):
                wait_s = _retry_after_seconds(resp, default=2 ** attempt)
                logger.warning(
                    "GitHub %s on %s; backing off %.1fs (attempt %s)",
                    resp.status_code,
                    url,
                    wait_s,
                    attempt + 1,
                )
                await asyncio.sleep(min(wait_s, 60))
            else:
                resp.raise_for_status()
                return resp

            attempt += 1
            if attempt >= max_retries:
                resp.raise_for_status()
                return resp

    # ---------- typed iterators ----------

    async def iter_issues(
        self, repo: str, state: str = "all", since: str | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield issues for `owner/repo`.

        Note: GitHub's `/issues` endpoint also returns pull requests (each carries
        a `pull_request` key).  We strip those out so callers can treat the streams
        independently.
        """
        params: dict[str, Any] = {"state": state, "sort": "created", "direction": "asc"}
        if since:
            params["since"] = since
        async for item in self._paged_get(f"/repos/{repo}/issues", params=params):
            if "pull_request" in item:
                continue
            yield item

    async def iter_pull_requests(
        self, repo: str, state: str = "all"
    ) -> AsyncIterator[dict[str, Any]]:
        params = {"state": state, "sort": "created", "direction": "asc"}
        async for item in self._paged_get(f"/repos/{repo}/pulls", params=params):
            yield item

    async def iter_issue_comments(
        self, repo: str, since: str | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """All issue/PR comments across the repo (single repo-wide endpoint)."""
        params: dict[str, Any] = {"sort": "created", "direction": "asc"}
        if since:
            params["since"] = since
        async for item in self._paged_get(
            f"/repos/{repo}/issues/comments", params=params
        ):
            yield item

    async def iter_pr_review_comments(
        self, repo: str, since: str | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Inline review comments across all PRs in the repo."""
        params: dict[str, Any] = {"sort": "created", "direction": "asc"}
        if since:
            params["since"] = since
        async for item in self._paged_get(
            f"/repos/{repo}/pulls/comments", params=params
        ):
            yield item


# ---------- helpers ----------

def _next_link(link_header: str | None) -> str | None:
    """Return the URL from a GitHub `Link` header rel=next entry, or None."""
    if not link_header:
        return None
    for chunk in link_header.split(","):
        parts = chunk.strip().split(";")
        if len(parts) < 2:
            continue
        url_part = parts[0].strip()
        rel_part = ";".join(parts[1:]).strip()
        if rel_part == 'rel="next"' and url_part.startswith("<") and url_part.endswith(">"):
            return url_part[1:-1]
    return None


def _is_auth_failure(resp: httpx.Response) -> bool:
    if resp.status_code == 401:
        return True
    msg = (resp.text or "").lower()
    return "bad credentials" in msg or "requires authentication" in msg


def _is_rate_limited(resp: httpx.Response) -> bool:
    remaining = resp.headers.get("X-RateLimit-Remaining")
    return remaining == "0"


def _retry_after_seconds(resp: httpx.Response, default: float) -> float:
    retry_after = resp.headers.get("Retry-After")
    if retry_after:
        try:
            return float(retry_after)
        except ValueError:
            pass
    reset = resp.headers.get("X-RateLimit-Reset")
    if reset:
        import time
        try:
            return max(1.0, float(reset) - time.time())
        except ValueError:
            pass
    return float(default)
