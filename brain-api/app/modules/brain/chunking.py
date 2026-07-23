"""Text chunking for the brain index (BRAIN_CHAT_RAG_PLAN P2/P3).

Long skill bodies / evidence spans are split into embeddable chunks so recall and
citations stay granular. Most skill logic is short (one chunk, ``chunk_index=0``);
this only kicks in for genuinely long documents. Splitting prefers paragraph then
sentence boundaries, and hard-splits only a single over-long run.
"""
from __future__ import annotations

import re

_MAX_CHARS = 1500


def chunk_text(text: str | None, *, max_chars: int = _MAX_CHARS) -> list[str]:
    """Split ``text`` into <= ``max_chars`` chunks on natural boundaries.

    Returns ``[]`` for empty/whitespace input (nothing to index). Deterministic, so
    the same body always yields the same chunk_index sequence (idempotent backfill).
    """
    if not text or not text.strip():
        return []
    body = text.strip()
    if len(body) <= max_chars:
        return [body]

    units = _split_units(body, max_chars)
    chunks: list[str] = []
    current = ""
    for unit in units:
        if current and len(current) + 1 + len(unit) > max_chars:
            chunks.append(current)
            current = unit
        else:
            current = f"{current}\n{unit}" if current else unit
    if current:
        chunks.append(current)
    return chunks


def _split_units(body: str, max_chars: int) -> list[str]:
    """Paragraphs, falling back to sentences, then a hard length split for any
    single run still longer than ``max_chars``."""
    units: list[str] = []
    for para in re.split(r"\n\s*\n", body):
        para = para.strip()
        if not para:
            continue
        if len(para) <= max_chars:
            units.append(para)
            continue
        for sentence in re.split(r"(?<=[.!?])\s+", para):
            sentence = sentence.strip()
            if not sentence:
                continue
            if len(sentence) <= max_chars:
                units.append(sentence)
            else:
                units.extend(
                    sentence[i : i + max_chars] for i in range(0, len(sentence), max_chars)
                )
    return units
