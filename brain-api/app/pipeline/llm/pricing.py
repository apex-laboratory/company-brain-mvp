"""Per-model $/Mtok price table + cost helper (PRD Phase 3 cost telemetry).

Prices are the public list prices at the time of writing; they exist to make
per-event/per-sweep cost rollups *comparable*, not to reconcile invoices.
Unknown models cost 0.0 and log once so a model bump can't silently break the
pipeline — update the table when changing ``settings.groq_model`` /
``anthropic_model`` / ``embedding_model``.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# model → (input $/Mtok, output $/Mtok)
_PRICES: dict[str, tuple[float, float]] = {
    "llama-3.3-70b-versatile": (0.59, 0.79),
    "claude-sonnet-5": (3.00, 15.00),
    "text-embedding-3-small": (0.02, 0.0),
}

_warned: set[str] = set()


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """Dollar cost of one call. Unknown model → 0.0 (warn once per process)."""
    prices = _PRICES.get(model)
    if prices is None:
        if model not in _warned:
            _warned.add(model)
            log.warning("pricing: unknown model %r — costs will read 0.0", model)
        return 0.0
    in_price, out_price = prices
    return (input_tokens * in_price + output_tokens * out_price) / 1_000_000


def ensure_priced(*models: str) -> None:
    """Raise if any configured model lacks a price entry.

    The price table is keyed by hardcoded model names while the model names are
    env-configurable (``settings.groq_model`` / ``anthropic_model`` /
    ``embedding_model``). Without this check, rotating a model via env silently
    makes every cost rollup read $0 while real spend continues. Call it at worker
    startup so a model bump fails loudly at boot instead of corrupting telemetry."""
    missing = sorted({m for m in models if m not in _PRICES})
    if missing:
        raise RuntimeError(
            f"pricing: no price entry for configured model(s) {missing}. "
            f"Add them to app/pipeline/llm/pricing.py._PRICES so cost telemetry "
            f"stays accurate."
        )
