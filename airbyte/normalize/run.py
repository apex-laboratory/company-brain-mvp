"""CLI for running post-sync normalization jobs.

Usage:
    python -m airbyte.normalize.run slack [--workspace acme]

Reads DATABASE_URL from the environment. Streams Airbyte raw rows out of the
staging tables and upserts canonical rows into raw_content.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import asyncpg

from . import slack

SOURCES = {
    "slack": slack.normalize,
}


async def _run(source: str, workspace: str | None) -> int:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2

    handler = SOURCES[source]
    conn = await asyncpg.connect(db_url)
    try:
        kwargs = {"workspace": workspace} if source == "slack" else {}
        counts = await handler(conn, **kwargs)
    finally:
        await conn.close()

    total = sum(counts.values())
    for stream, n in counts.items():
        print(f"  {stream:20s} {n:>6d}")
    print(f"  {'total':20s} {total:>6d}")
    return 0 if total > 0 else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", choices=sorted(SOURCES.keys()))
    parser.add_argument(
        "--workspace",
        help="Slack workspace subdomain (used to build permalink URLs).",
        default=os.environ.get("SLACK_WORKSPACE"),
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(_run(args.source, args.workspace)))


if __name__ == "__main__":
    main()
