"""Incremental parse of the synthesizer's streamed JSON reply.

The synthesizer answers with one JSON object whose keys are emitted in a fixed
order — ``grounded``, ``confidence``, ``usedSkillIds``, then ``answer`` **last**.
That ordering is what makes token streaming safe: the grounding verdict arrives
*before* any answer text, so the caller can decide whether to stream tokens at all
rather than streaming a confident-looking answer it would then have to retract.

Usage: ``feed()`` each raw chunk; it returns the newly decoded answer text (``""``
until the answer string opens). ``head`` is populated as soon as the pre-answer keys
are complete. Tolerant of markdown code fences and of chunk boundaries splitting a
JSON escape — a partial ``\\uXXXX`` is held back until its digits arrive.
"""
from __future__ import annotations

import json
import re
from typing import Any

# The opening of the answer string value; ``end()`` lands just past its quote.
_ANSWER_KEY = re.compile(r'"answer"\s*:\s*"')

_ESCAPES = {
    '"': '"', "\\": "\\", "/": "/",
    "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t",
}


def _parse_head(text: str) -> dict[str, Any]:
    """Parse the object's pre-``answer`` keys by closing the partial JSON.

    ``text`` is everything before the ``"answer"`` key — e.g.
    ``{"grounded": true, "confidence": 0.82, "usedSkillIds": [],``. Dropping the
    trailing comma and appending ``}`` makes it valid on its own. Returns ``{}`` if
    it can't be parsed (the caller falls back to the non-streaming contract).
    """
    start = text.find("{")
    if start == -1:
        return {}
    candidate = text[start:].strip()
    if candidate.endswith(","):
        candidate = candidate[:-1]
    try:
        parsed = json.loads(candidate + "}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


class GroundedAnswerParser:
    """Streaming reader for the synthesizer's ordered JSON object."""

    def __init__(self) -> None:
        self._raw = ""
        self._cursor = 0  # index into _raw; only meaningful once inside the answer
        self._head: dict[str, Any] | None = None
        self._in_answer = False
        self._complete = False
        self._parts: list[str] = []

    @property
    def head(self) -> dict[str, Any] | None:
        """The pre-answer keys, or ``None`` until they've fully arrived."""
        return self._head

    @property
    def answer(self) -> str:
        """Everything decoded so far."""
        return "".join(self._parts)

    @property
    def complete(self) -> bool:
        """Whether the answer string's closing quote has been seen."""
        return self._complete

    def feed(self, chunk: str) -> str:
        """Consume a raw chunk; return newly decoded answer text (may be ``""``)."""
        self._raw += chunk
        if not self._in_answer:
            self._try_open_answer()
        if self._in_answer and not self._complete:
            return self._consume()
        return ""

    def _try_open_answer(self) -> None:
        match = _ANSWER_KEY.search(self._raw)
        if match is None:
            return
        self._head = _parse_head(self._raw[: match.start()])
        self._cursor = match.end()
        self._in_answer = True

    def _consume(self) -> str:
        """Decode answer characters from the cursor, stopping at the closing quote.

        Stops early (without advancing) on a truncated escape so the next chunk can
        complete it.
        """
        out: list[str] = []
        raw, i, n = self._raw, self._cursor, len(self._raw)
        while i < n:
            ch = raw[i]
            if ch == "\\":
                if i + 1 >= n:
                    break  # escape char not yet received
                esc = raw[i + 1]
                if esc == "u":
                    if i + 6 > n:
                        break  # \uXXXX not yet complete
                    decoded, consumed = self._unescape_unicode(raw, i, n)
                    if decoded is None:
                        break  # surrogate pair still incomplete
                    out.append(decoded)
                    i += consumed
                    continue
                out.append(_ESCAPES.get(esc, esc))
                i += 2
                continue
            if ch == '"':
                self._complete = True
                i += 1
                break
            out.append(ch)
            i += 1
        self._cursor = i
        text = "".join(out)
        if text:
            self._parts.append(text)
        return text

    @staticmethod
    def _unescape_unicode(raw: str, i: int, n: int) -> tuple[str | None, int]:
        """Decode ``\\uXXXX`` at ``i``, joining a surrogate pair when present.

        Returns ``(None, 0)`` if a high surrogate's partner hasn't arrived yet.
        """
        try:
            code = int(raw[i + 2 : i + 6], 16)
        except ValueError:
            return raw[i + 2 : i + 6], 6  # malformed — pass the digits through
        if 0xD800 <= code <= 0xDBFF:  # high surrogate: needs the following \uXXXX
            if i + 12 > n:
                return None, 0
            if raw[i + 6 : i + 8] != "\\u":
                return chr(code), 6
            try:
                low = int(raw[i + 8 : i + 12], 16)
            except ValueError:
                return chr(code), 6
            if 0xDC00 <= low <= 0xDFFF:
                combined = 0x10000 + (code - 0xD800) * 0x400 + (low - 0xDC00)
                return chr(combined), 12
            return chr(code), 6
        return chr(code), 6
