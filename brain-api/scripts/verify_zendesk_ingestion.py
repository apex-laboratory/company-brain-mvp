"""
End-to-end smoke test for Zendesk ingestion.

Steps:
1. Load the bundled fixture
2. Run the pure normalizer over each record
3. Validate every row against the canonical contract
4. (Optional) Upsert into Postgres and print the resulting event log

Pure-mapping pass (steps 1-3) runs with zero dependencies beyond the project
requirements. The DB pass (step 4) only runs when DATABASE_URL is reachable.

Run from the repo root:

    python -m brain-api.scripts.verify_zendesk_ingestion          # pure pass
    python -m brain-api.scripts.verify_zendesk_ingestion --db     # + DB pass

Or via Docker once `brain-api` is up:

    docker compose exec brain-api python scripts/verify_zendesk_ingestion.py --db
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

# Allow running both as `python -m brain-api.scripts.verify_zendesk_ingestion`
# from the repo root and as `python scripts/verify_zendesk_ingestion.py` from
# inside the brain-api container (where `/app` is the working dir).
BRAIN_API_ROOT = Path(__file__).resolve().parent.parent
if str(BRAIN_API_ROOT) not in sys.path:
    sys.path.insert(0, str(BRAIN_API_ROOT))


FIXTURE_PATH = BRAIN_API_ROOT / "fixtures" / "zendesk_sample.json"


def run_pure_pass() -> list[dict]:
    from services import zendesk_normalizer

    payload = json.loads(FIXTURE_PATH.read_text())
    subdomain = payload.get("subdomain")

    rows: list[dict] = []
    for t in payload.get("tickets", []):
        t = {**t, "subdomain": subdomain} if subdomain and "subdomain" not in t else t
        rows.append(zendesk_normalizer.normalize_ticket(t))
    for c in payload.get("comments", []):
        c = {**c, "subdomain": subdomain} if subdomain and "subdomain" not in c else c
        rows.append(zendesk_normalizer.normalize_comment(c))
    for e in payload.get("events", []):
        e = {**e, "subdomain": subdomain} if subdomain and "subdomain" not in e else e
        rows.append(zendesk_normalizer.normalize_event(e))

    print(f"[pure] normalized {len(rows)} raw_content rows from fixture")
    for row in rows:
        zendesk_normalizer.validate_row(row)
    print(f"[pure] all rows pass the canonical contract validation")

    counts: dict[str, int] = {}
    for row in rows:
        et = row["metadata"]["entity_type"]
        counts[et] = counts.get(et, 0) + 1
    print(f"[pure] by entity_type: {counts}")

    sample = rows[0]
    print("[pure] sample row:")
    print(json.dumps(sample, indent=2, default=str)[:600])
    return rows


async def run_db_pass(rows: list[dict]) -> None:
    import asyncpg

    from services import process_miner, zendesk_normalizer

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("[db] DATABASE_URL not set — skipping DB pass", file=sys.stderr)
        return

    # asyncpg accepts a postgresql:// DSN directly.
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        result = await zendesk_normalizer.upsert_rows(pool, rows)
        print(f"[db] upsert result: {result}")

        async with pool.acquire() as conn:
            total = await conn.fetchval(
                "SELECT COUNT(*) FROM raw_content WHERE source = 'zendesk'"
            )
            by_entity = await conn.fetch(
                """
                SELECT metadata->>'entity_type' AS entity_type, COUNT(*) AS n
                FROM raw_content
                WHERE source = 'zendesk'
                GROUP BY 1
                ORDER BY 1
                """
            )
        print(f"[db] raw_content zendesk total: {total}")
        for row in by_entity:
            print(f"[db]   {row['entity_type']}: {row['n']}")

        log = await process_miner.event_log_as_json(pool)
        print(f"[db] event log cases={log['cases']} events={len(log['rows'])}")
        for row in log["rows"][:5]:
            print(
                f"[db]   {row['case_id']:<14} {row['activity']:<24} {row['timestamp']}"
            )

        variants = await process_miner.mine_zendesk_patterns(pool)
        print(f"[db] variants discovered: {len(variants['variants'])}")
        for v in variants["variants"][:5]:
            print(f"[db]   x{v['count']}: {v['trace']}")
    finally:
        await pool.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--db",
        action="store_true",
        help="Also upsert into Postgres and dump the event log",
    )
    args = parser.parse_args()

    rows = run_pure_pass()
    if args.db:
        asyncio.run(run_db_pass(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
