"""Idempotent upserts into the canonical `raw_content` table.

Keyed on the (source, source_id) UNIQUE constraint defined in schema.sql.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from typing import Any

from database import get_pool

logger = logging.getLogger(__name__)


_UPSERT_SQL = """
INSERT INTO raw_content (source, source_id, content, metadata, ingested_at, updated_at)
VALUES ($1, $2, $3, $4::jsonb, NOW(), NOW())
ON CONFLICT (source, source_id) DO UPDATE
   SET content    = EXCLUDED.content,
       metadata   = EXCLUDED.metadata,
       updated_at = NOW()
RETURNING (xmax = 0) AS inserted;
"""


async def upsert_rows(rows: Iterable[Any]) -> tuple[int, int]:
    """Upsert canonical rows. Returns (inserted_count, updated_count).

    Each row may be a `RawContentRow` dataclass or any object exposing
    `.source`, `.source_id`, `.content`, `.metadata` attributes.
    """
    rows = list(rows)
    if not rows:
        return (0, 0)

    pool = await get_pool()
    inserted = 0
    updated = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            for row in rows:
                payload = json.dumps(row.metadata or {}, default=str)
                was_inserted = await conn.fetchval(
                    _UPSERT_SQL,
                    row.source,
                    row.source_id,
                    row.content,
                    payload,
                )
                if was_inserted:
                    inserted += 1
                else:
                    updated += 1
    logger.info(
        "raw_content upsert: inserted=%s updated=%s total=%s",
        inserted,
        updated,
        len(rows),
    )
    return inserted, updated
