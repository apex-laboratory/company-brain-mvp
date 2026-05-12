"""Normalize Airbyte Slack raw staging tables into canonical raw_content rows.

Implements the Slack section of airbyte/raw-content-contract.md (L1-04):

- channel messages → entity_type=message
- thread replies   → entity_type=thread_reply

Airbyte Destinations V2 lands raw rows in `airbyte_internal.<source>_raw__stream_<name>`
with an `_airbyte_data` JSONB column. Older deployments land them in the target
schema as `_airbyte_raw_<name>`. Both layouts are accepted via SOURCE_TABLES.

The normalizer is idempotent: each (source, source_id) is removed before insert
within the same transaction, so re-running after another sync overwrites in
place. We avoid an ON CONFLICT clause because raw_content currently has no
unique constraint on (source, source_id).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, AsyncIterator

SOURCE = "slack"

# (logical stream name, candidate fully qualified table names in priority order)
SOURCE_TABLES: dict[str, list[str]] = {
    "channel_messages": [
        "airbyte_internal.slack_raw__stream_channel_messages",
        "public._airbyte_raw_channel_messages",
        "public.channel_messages",
    ],
    "threads": [
        "airbyte_internal.slack_raw__stream_threads",
        "public._airbyte_raw_threads",
        "public.threads",
    ],
    "channels": [
        "airbyte_internal.slack_raw__stream_channels",
        "public._airbyte_raw_channels",
        "public.channels",
    ],
    "users": [
        "airbyte_internal.slack_raw__stream_users",
        "public._airbyte_raw_users",
        "public.users",
    ],
}

# Slack subtypes that are transport noise — joins, leaves, channel rename, etc.
NOISE_SUBTYPES = {
    "channel_join",
    "channel_leave",
    "channel_topic",
    "channel_purpose",
    "channel_name",
    "channel_archive",
    "channel_unarchive",
    "bot_add",
    "bot_remove",
}

_USER_MENTION = re.compile(r"<@([UW][A-Z0-9]+)>")


@dataclass
class CanonicalRow:
    source: str
    source_id: str
    content: str
    metadata: dict[str, Any]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ts_to_permalink_segment(ts: str) -> str:
    """Slack permalinks use the ts with the dot stripped, prefixed with `p`."""
    return "p" + ts.replace(".", "")


def _permalink(workspace: str | None, channel_id: str, ts: str, thread_ts: str | None) -> str | None:
    if not workspace:
        return None
    base = f"https://{workspace}.slack.com/archives/{channel_id}/{_ts_to_permalink_segment(ts)}"
    if thread_ts and thread_ts != ts:
        base += f"?thread_ts={thread_ts}&cid={channel_id}"
    return base


def _resolve_mentions(text: str, users: dict[str, dict[str, Any]]) -> str:
    def repl(m: re.Match[str]) -> str:
        u = users.get(m.group(1))
        if not u:
            return m.group(0)
        handle = u.get("name") or u.get("real_name")
        return f"@{handle}" if handle else m.group(0)

    return _USER_MENTION.sub(repl, text)


def _author(user_id: str | None, users: dict[str, dict[str, Any]]) -> dict[str, Any]:
    user = users.get(user_id or "", {})
    profile = user.get("profile") or {}
    return {
        "id": user_id,
        "name": user.get("real_name") or profile.get("real_name"),
        "email": profile.get("email"),
        "handle": user.get("name"),
    }


# ---------------------------------------------------------------------------
# Pure normalizers
# ---------------------------------------------------------------------------

def normalize_message(
    record: dict[str, Any],
    *,
    channels: dict[str, dict[str, Any]],
    users: dict[str, dict[str, Any]],
    workspace: str | None,
) -> CanonicalRow | None:
    if record.get("subtype") in NOISE_SUBTYPES:
        return None

    ts = record.get("ts")
    channel_id = record.get("channel_id") or record.get("channel")
    if not ts or not channel_id:
        return None

    text = (record.get("text") or "").strip()
    if not text:
        return None

    thread_ts = record.get("thread_ts")
    is_thread_reply = bool(thread_ts) and thread_ts != ts
    is_thread_root = bool(thread_ts) and thread_ts == ts

    channel = channels.get(channel_id, {})
    channel_name = channel.get("name")

    parent_source_id = (
        f"slack_message:{channel_id}:{thread_ts}" if is_thread_reply else None
    )

    metadata = {
        "entity_type": "thread_reply" if is_thread_reply else "message",
        "record_url": _permalink(workspace, channel_id, ts, thread_ts),
        "title": None,
        "author": _author(record.get("user"), users),
        "created_at": _ts_to_iso(ts),
        "updated_at": _ts_to_iso(record.get("edited", {}).get("ts") or ts),
        "tags": [channel_name] if channel_name else [],
        "airbyte_stream": "channel_messages" if not is_thread_reply else "threads",
        "channel_id": channel_id,
        "channel_name": channel_name,
        "thread_id": thread_ts,
        "parent_source_id": parent_source_id,
        "is_thread_root": is_thread_root,
        "raw_id": ts,
    }

    return CanonicalRow(
        source=SOURCE,
        source_id=f"slack_message:{channel_id}:{ts}",
        content=_resolve_mentions(text, users),
        metadata=metadata,
    )


def _ts_to_iso(ts: str | None) -> str | None:
    if not ts:
        return None
    try:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# DB I/O
# ---------------------------------------------------------------------------

DELETE_SQL = "DELETE FROM raw_content WHERE source = $1 AND source_id = $2"
INSERT_SQL = (
    "INSERT INTO raw_content (source, source_id, content, metadata) "
    "VALUES ($1, $2, $3, $4::jsonb)"
)


async def _resolve_table(conn, candidates: list[str]) -> str | None:
    for fq in candidates:
        exists = await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", fq)
        if exists:
            return fq
    return None


async def _iter_raw_records(conn, table: str) -> AsyncIterator[dict[str, Any]]:
    has_raw_col = await conn.fetchval(
        """
        SELECT EXISTS (
          SELECT 1 FROM information_schema.columns
          WHERE table_schema = split_part($1, '.', 1)
            AND table_name   = split_part($1, '.', 2)
            AND column_name  = '_airbyte_data'
        )
        """,
        table,
    )
    sql = (
        f"SELECT _airbyte_data AS data FROM {table}"
        if has_raw_col
        else f"SELECT row_to_json(t)::jsonb AS data FROM {table} t"
    )
    async with conn.transaction():
        async for row in conn.cursor(sql):
            data = row["data"]
            if isinstance(data, str):
                data = json.loads(data)
            yield data


async def _load_lookup(conn, candidates: list[str], key: str) -> dict[str, dict[str, Any]]:
    """Materialize a small lookup table (channels, users) into memory by `key`."""
    table = await _resolve_table(conn, candidates)
    if not table:
        return {}
    out: dict[str, dict[str, Any]] = {}
    async for record in _iter_raw_records(conn, table):
        k = record.get(key) or record.get("id")
        if k:
            out[k] = record
    return out


async def normalize(conn, *, workspace: str | None = None) -> dict[str, int]:
    """Run Slack normalization end to end. Returns per-stream upsert counts."""

    channels = await _load_lookup(conn, SOURCE_TABLES["channels"], key="id")
    users = await _load_lookup(conn, SOURCE_TABLES["users"], key="id")

    counts: dict[str, int] = {}

    for stream in ("channel_messages", "threads"):
        table = await _resolve_table(conn, SOURCE_TABLES[stream])
        if not table:
            counts[stream] = 0
            continue

        n = 0
        async with conn.transaction():
            async for record in _iter_raw_records(conn, table):
                canonical = normalize_message(
                    record, channels=channels, users=users, workspace=workspace
                )
                if canonical is None:
                    continue
                await conn.execute(DELETE_SQL, canonical.source, canonical.source_id)
                await conn.execute(
                    INSERT_SQL,
                    canonical.source,
                    canonical.source_id,
                    canonical.content,
                    json.dumps(canonical.metadata, default=str),
                )
                n += 1
        counts[stream] = n

    return counts
