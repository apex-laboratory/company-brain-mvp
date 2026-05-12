"""One-shot GitHub historical sync.

Run from the repo root with the brain-api venv active:

    cd brain-api
    python -m scripts.sync_github --repos octocat/Hello-World

Or, relying on .env:

    cd brain-api
    python -m scripts.sync_github
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

# Allow `python scripts/sync_github.py` from the brain-api/ directory.
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import configured_github_repos, settings  # noqa: E402
from database import close_db_pool, init_db_pool  # noqa: E402
from services.github_sync import sync_repos  # noqa: E402


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="GitHub → raw_content historical sync")
    p.add_argument(
        "--repos",
        help="Comma-separated owner/repo slugs (defaults to GITHUB_REPOS env)",
        default=None,
    )
    p.add_argument(
        "--state",
        choices=("open", "closed", "all"),
        default="all",
        help="Issue / PR state filter",
    )
    p.add_argument(
        "--since",
        help="ISO-8601 timestamp; only fetch records updated after this point",
        default=None,
    )
    return p.parse_args()


async def _main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = _parse_args()

    repos = (
        [r.strip() for r in args.repos.split(",") if r.strip()]
        if args.repos
        else configured_github_repos()
    )
    if not repos:
        print(
            "ERROR: no repos given (use --repos owner/name or set GITHUB_REPOS).",
            file=sys.stderr,
        )
        return 2
    if not settings.github_token:
        print(
            "ERROR: GITHUB_TOKEN is not set. Add it to your .env before running.",
            file=sys.stderr,
        )
        return 2

    await init_db_pool()
    try:
        results = await sync_repos(
            repos=repos,
            state=args.state,
            since=args.since,
            token=settings.github_token,
        )
    finally:
        await close_db_pool()

    payload = {"repos": [r.to_dict() for r in results]}
    print(json.dumps(payload, indent=2))
    has_errors = any(r.errors for r in results)
    return 1 if has_errors else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
