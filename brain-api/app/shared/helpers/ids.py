"""Prefixed ULID generation (BACKEND_BEST_PRACTICES.md §3).

IDs are opaque, prefixed, and stable so they are safe to expose to clients and
sort lexicographically by creation time (ULID property). Raw auto-increment
integers are never exposed.
"""
from __future__ import annotations

import ulid

PREFIX_MAP: dict[str, str] = {
    "user": "usr",
    "workspace": "wrk",
    "source": "src",
    "decision": "dec",
    "review": "rev",
    "skill": "skl",
    "key": "key",
    "invite": "inv",
    "conversation": "cnv",
    "message": "msg",
    "build": "bld",
    "activity": "act",
    "member": "mem",
    "channel": "chn",
    "request": "req",
    "refresh_token": "rt",
    "oauth_state": "st",
}


def generate_id(entity: str) -> str:
    """Return a prefixed, lowercased ULID for ``entity`` (e.g. ``usr_01h...``)."""
    try:
        prefix = PREFIX_MAP[entity]
    except KeyError as exc:
        raise KeyError(f"Unknown id entity: {entity!r}") from exc
    return f"{prefix}_{ulid.new().str.lower()}"
