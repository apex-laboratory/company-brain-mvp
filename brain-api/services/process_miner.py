"""
PM4Py process mining for Zendesk ticket events.

Phase 2 deliverable: build a PM4Py-shaped event log from `raw_content` rows
where `metadata.entity_type = 'ticket_event'`. Each row is one activity in
the case of its parent ticket.

Phase 3 will plug actual mining algorithms (alpha miner, heuristic miner,
inductive miner) on top of this event log. The DataFrame produced here is
already in the canonical PM4Py shape (`case:concept:name`, `concept:name`,
`time:timestamp`), so the downstream call is a one-liner.
"""

from __future__ import annotations

import json
from typing import Any

import pandas as pd


PM4PY_CASE_COL = "case:concept:name"
PM4PY_ACTIVITY_COL = "concept:name"
PM4PY_TIMESTAMP_COL = "time:timestamp"


_EVENT_LOG_SQL = """
SELECT
    source_id,
    metadata->>'parent_source_id'  AS case_id,
    metadata->>'field_name'        AS field_name,
    metadata->>'previous_value'    AS previous_value,
    metadata->>'new_value'         AS new_value,
    metadata->>'event_type'        AS event_type,
    metadata->'author'->>'id'      AS author_id,
    COALESCE(
        metadata->>'source_timestamp',
        metadata->>'created_at'
    )                              AS event_timestamp
FROM raw_content
WHERE source = 'zendesk'
  AND metadata->>'entity_type' = 'ticket_event'
  AND ($1::text[] IS NULL OR metadata->>'parent_source_id' = ANY($1))
ORDER BY case_id ASC,
         event_timestamp ASC
"""


def _activity_name(field_name: str | None, new_value: str | None) -> str:
    """
    Build a PM4Py activity label from a field transition.

    Activity granularity is `{field}:{new_value}` so the alpha miner can
    discover the distinct state machine per field. Free-form fields (subject,
    description) collapse to `{field}:changed` to avoid cardinality blowup.
    """
    field = (field_name or "unknown").strip()
    if not new_value:
        return f"{field}:cleared"
    if field in {"subject", "description", "comment", "tags"}:
        return f"{field}:changed"
    return f"{field}:{new_value}"


async def fetch_event_log(
    pool,
    ticket_source_ids: list[str] | None = None,
) -> pd.DataFrame:
    """
    Fetch ticket events from `raw_content` and return a PM4Py-shaped DataFrame.

    `ticket_source_ids`, if provided, must be canonical `ticket:<id>` strings —
    matching `metadata.parent_source_id` on event rows.
    """
    async with pool.acquire() as conn:
        records = await conn.fetch(_EVENT_LOG_SQL, ticket_source_ids)

    if not records:
        return pd.DataFrame(
            columns=[
                PM4PY_CASE_COL,
                PM4PY_ACTIVITY_COL,
                PM4PY_TIMESTAMP_COL,
                "field_name",
                "previous_value",
                "new_value",
                "author_id",
                "source_id",
            ]
        )

    df = pd.DataFrame([dict(r) for r in records])
    df[PM4PY_TIMESTAMP_COL] = pd.to_datetime(df["event_timestamp"], utc=True, errors="coerce")
    df[PM4PY_CASE_COL] = df["case_id"]
    df[PM4PY_ACTIVITY_COL] = df.apply(
        lambda r: _activity_name(r["field_name"], r["new_value"]),
        axis=1,
    )
    df = df.dropna(subset=[PM4PY_TIMESTAMP_COL, PM4PY_CASE_COL])
    df = df.sort_values([PM4PY_CASE_COL, PM4PY_TIMESTAMP_COL]).reset_index(drop=True)
    return df[
        [
            PM4PY_CASE_COL,
            PM4PY_ACTIVITY_COL,
            PM4PY_TIMESTAMP_COL,
            "field_name",
            "previous_value",
            "new_value",
            "author_id",
            "source_id",
        ]
    ]


def to_pm4py_event_log(df: pd.DataFrame):
    """
    Convert the canonical DataFrame to a PM4Py EventLog object.

    Import is deferred so the rest of the module stays usable when pm4py
    is not installed (e.g. during light-touch normalization tests).
    """
    from pm4py.objects.conversion.log import converter as log_converter

    return log_converter.apply(df, variant=log_converter.Variants.TO_EVENT_LOG)


async def mine_zendesk_patterns(pool, ticket_source_ids: list[str] | None = None) -> dict[str, Any]:
    """
    Discover dominant ticket-handling variants from the event log.

    For MVP we return variant counts (PM4Py's `pm4py.stats.get_variants`).
    Phase 3 will swap this for a full alpha/inductive miner pass and
    convert frequent variants into rule candidates.
    """
    df = await fetch_event_log(pool, ticket_source_ids)
    if df.empty:
        return {"cases": 0, "events": 0, "variants": [], "activities": []}

    activities = sorted(df[PM4PY_ACTIVITY_COL].unique().tolist())

    variants: dict[str, int] = {}
    for case_id, group in df.groupby(PM4PY_CASE_COL, sort=False):
        trace = tuple(group[PM4PY_ACTIVITY_COL].tolist())
        variants[" → ".join(trace)] = variants.get(" → ".join(trace), 0) + 1

    top_variants = sorted(variants.items(), key=lambda kv: kv[1], reverse=True)

    return {
        "cases": int(df[PM4PY_CASE_COL].nunique()),
        "events": int(len(df)),
        "activities": activities,
        "variants": [
            {"trace": trace, "count": count} for trace, count in top_variants[:20]
        ],
    }


async def event_log_as_json(
    pool, ticket_source_ids: list[str] | None = None
) -> dict[str, Any]:
    """Serialize the event log for API responses."""
    df = await fetch_event_log(pool, ticket_source_ids)
    if df.empty:
        return {"rows": [], "cases": 0, "activities": [], "format": "pm4py-dataframe"}

    rows = []
    for _, r in df.iterrows():
        rows.append(
            {
                "case_id": r[PM4PY_CASE_COL],
                "activity": r[PM4PY_ACTIVITY_COL],
                "timestamp": r[PM4PY_TIMESTAMP_COL].isoformat(),
                "field_name": r["field_name"],
                "previous_value": r["previous_value"],
                "new_value": r["new_value"],
                "author_id": r["author_id"],
                "source_id": r["source_id"],
            }
        )
    return {
        "rows": rows,
        "cases": int(df[PM4PY_CASE_COL].nunique()),
        "activities": sorted(df[PM4PY_ACTIVITY_COL].unique().tolist()),
        "format": "pm4py-dataframe",
    }
