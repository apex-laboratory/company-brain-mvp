from fastapi import APIRouter, HTTPException

from config import configured_github_repos, settings
from models.schemas import (
    GitHubRepoSyncResult,
    GitHubStreamCounts,
    GitHubSyncRequest,
    GitHubSyncResponse,
)
from services.github_sync import sync_repos

router = APIRouter()


@router.post("/event")
async def ingest_event():
    raise HTTPException(status_code=501, detail="Not implemented — Phase 5")


@router.post("/batch")
async def ingest_batch():
    raise HTTPException(status_code=501, detail="Not implemented — Phase 3")


@router.post("/github/sync", response_model=GitHubSyncResponse)
async def ingest_github_sync(req: GitHubSyncRequest | None = None) -> GitHubSyncResponse:
    """Run a GitHub historical/incremental sync into raw_content.

    Phase 2 (L1-06).  Pulls issues, pull requests, issue comments and PR review
    comments for each `owner/repo` in the request body — or, if omitted, the
    GITHUB_REPOS env var.  Idempotent: re-running upserts on (source, source_id).
    """
    req = req or GitHubSyncRequest()
    repos = req.repos or configured_github_repos()
    if not repos:
        raise HTTPException(
            status_code=400,
            detail="No repos provided and GITHUB_REPOS env var is empty.",
        )
    if not settings.github_token:
        # Public-repo only mode is possible but heavily rate-limited (60 req/h);
        # surface this as a 400 so a teammate doesn't silently hit the limit.
        raise HTTPException(
            status_code=400,
            detail="GITHUB_TOKEN is not set — refusing unauthenticated sync.",
        )

    results = await sync_repos(
        repos=repos,
        state=req.state,
        since=req.since,
        token=settings.github_token,
    )
    return GitHubSyncResponse(
        repos=[
            GitHubRepoSyncResult(
                repo=r.repo,
                streams={
                    name: GitHubStreamCounts(
                        fetched=s.fetched, inserted=s.inserted, updated=s.updated
                    )
                    for name, s in r.streams.items()
                },
                errors=r.errors,
            )
            for r in results
        ]
    )
