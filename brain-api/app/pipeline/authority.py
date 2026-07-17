"""Source-authority annotation + routing/sweep config from ``source_authority.yaml``.

Port of ``services/authority_annotator.py`` onto app settings, extended with the
``routing:`` and ``sweep:`` sections the pipeline needs. Fail-soft like
``app/jobs/sweep_order.py``: a missing/malformed file falls back to built-in
defaults so a config mistake degrades authority precision, never extraction.

Authority **tier** (high/medium/low) feeds ``confidence_scorer`` multipliers and
the routing floor; the YAML tier **weight** (1.0/0.7/0.4) is injected into the
extraction prompt as a prompt-weighting signal only — it is NOT the routing
multiplier (those are PRD Feature 9's 1.0/0.85/0.65 in ``confidence_scorer``).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

from app.config.settings import settings

log = logging.getLogger(__name__)

_DEFAULT_WEIGHTS = {"high": 1.0, "medium": 0.7, "low": 0.4}
_DEFAULT_ORDER = ["notion", "google_drive", "gmail", "github", "jira", "slack", "zendesk"]


@dataclass(frozen=True)
class AuthorityAnnotation:
    tier: str  # "high" | "medium" | "low"
    weight: float  # YAML tier weight (prompt signal, not routing multiplier)


@dataclass(frozen=True)
class RoutingConfig:
    auto_publish_confidence: float = 0.90
    auto_publish_authority_floor: str = "medium"
    review_queue_confidence_floor: float = 0.70


@dataclass(frozen=True)
class SweepConfig:
    rate_per_minute: int = 10
    semaphore_limit: int = 5


class AuthorityAnnotator:
    def __init__(self, config_path: str | Path | None = None) -> None:
        path = Path(config_path or settings.source_authority_path)
        try:
            config = yaml.safe_load(path.read_text())
            if not isinstance(config, dict):
                raise ValueError("source_authority.yaml is not a mapping")
        except Exception:
            log.warning(
                "authority: could not read %s; using built-in defaults", path,
                exc_info=True,
            )
            config = {}
        self._config: dict = config

    # ── public ──────────────────────────────────────────────────────────────

    def annotate(self, source: str, payload: dict) -> AuthorityAnnotation:
        """Return the highest matching authority tier for a source item."""
        tiers: dict = self._config.get("tiers") or {}
        for tier_name, tier_cfg in tiers.items():
            entries = [e for e in tier_cfg.get("sources", []) if e.get("type") == source]
            if any(self._match_signals(e.get("signals", []), payload) for e in entries):
                return AuthorityAnnotation(
                    tier=tier_name,
                    weight=float(tier_cfg.get("weight", _DEFAULT_WEIGHTS.get(tier_name, 0.4))),
                )
        low = tiers.get("low") or {}
        return AuthorityAnnotation(
            tier="low", weight=float(low.get("weight", _DEFAULT_WEIGHTS["low"]))
        )

    def routing_config(self) -> RoutingConfig:
        routing = self._config.get("routing") or {}
        defaults = RoutingConfig()
        return RoutingConfig(
            auto_publish_confidence=float(
                routing.get("auto_publish_confidence", defaults.auto_publish_confidence)
            ),
            auto_publish_authority_floor=str(
                routing.get("auto_publish_authority_floor", defaults.auto_publish_authority_floor)
            ),
            review_queue_confidence_floor=float(
                routing.get("review_queue_confidence_floor", defaults.review_queue_confidence_floor)
            ),
        )

    def processing_order(self) -> list[str]:
        """Provider names in sweep priority order (always includes every known
        provider — ones the YAML omits are appended in the built-in default order).

        Same parsed config + fail-soft policy as the rest of this loader, so the
        sweep order and the authority tiers can't disagree about the file's state."""
        sweep = self._config.get("sweep") or {}
        order = [str(p) for p in (sweep.get("processing_order") or [])]
        return order + [p for p in _DEFAULT_ORDER if p not in order]

    def sweep_config(self) -> SweepConfig:
        sweep = self._config.get("sweep") or {}
        defaults = SweepConfig()
        return SweepConfig(
            rate_per_minute=int(sweep.get("rate_per_minute", defaults.rate_per_minute)),
            semaphore_limit=int(sweep.get("semaphore_limit", defaults.semaphore_limit)),
        )

    # ── private ─────────────────────────────────────────────────────────────

    def _match_signals(self, signals: list[str], payload: dict) -> bool:
        """True if any signal is satisfied by payload (empty list = catch-all)."""
        if not signals:
            return True
        return any(self._check_signal(s, payload) for s in signals)

    def _check_signal(self, signal: str, payload: dict) -> bool:
        if "=" not in signal:
            return bool(payload.get(signal, False))
        key, value = signal.split("=", 1)
        if key == "path_prefix":
            return str(payload.get("path", "")).startswith(value)
        if key == "tag":
            # `or []` guards an explicit `"tags": null` (fail-soft: fall through to
            # tier low, never TypeError → dead-letter the whole event).
            return value in (payload.get("tags") or [])
        return str(payload.get(key, "")).lower() == value.lower()


# ── module-level singleton ───────────────────────────────────────────────────

_annotator: AuthorityAnnotator | None = None


def get_annotator() -> AuthorityAnnotator:
    global _annotator
    if _annotator is None:
        _annotator = AuthorityAnnotator()
    return _annotator


def annotate(source: str, payload: dict) -> AuthorityAnnotation:
    return get_annotator().annotate(source, payload)


def routing_config() -> RoutingConfig:
    return get_annotator().routing_config()


def sweep_config() -> SweepConfig:
    return get_annotator().sweep_config()


def processing_order() -> list[str]:
    return get_annotator().processing_order()
